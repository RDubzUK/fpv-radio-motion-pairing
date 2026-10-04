# FPV / StickCam: complete workflow plan

Updated: 4 October 2026. Current review: [WORKFLOW_REVIEW.md](WORKFLOW_REVIEW.md). Earlier development and the known-pair investigation: [PROJECT_HISTORY.md](PROJECT_HISTORY.md).

## Intended result

A saved session turns a folder of FPV recordings and a folder of StickCam recordings into content-supported pairing proposals, synchronized review, and aligned exports or a portrait edit. The normal workload is 10–40 flights, often 1080p–4K, with unequal recording lengths and potentially several O4 FPV files for one StickCam. The default path should require only source selection, exception handling and a short alignment review. Technical controls remain available when useful.

“No touch” means avoid unnecessary calibration and repeated decoding, retain progress and explain exceptions. It does not mean automatically approving uncertain pairs. A safe unmatched result is preferable to an incorrect aligned export.

## The process now implemented

1. **Name the session and choose sources.** Browse server folders, including mounted SMB shares, or upload resumably. Keep sources read in place. A collapsed video list supports all, some or one target from either side. A source summary allows availability checks and relinking existing filenames after a drive/share moves.
2. **Find flights automatically.** Read metadata and saved boundary-audio fingerprints first. Check distinct spectral landmarks, independently agreeing audio trends, repeated 3-second sections, competing offsets and competing StickCam owners. Distinctive proposals skip unnecessary vision. Audio identifies content; timestamps establish relative camera clocks only after a content anchor. Dates can be wrong or reset to 1970. Duration only defines possible overlap.
3. **Prepare unresolved evidence.** Reuse current calibrated StickCam input data. Use embedded FPV gyro/attitude before any visual fallback. Unconfigured or invalid sources remain in the results with a reason. Without trusted StickCam calibration, complete audio review and defer expensive FPV vision. Check selected FPV motion availability before deriving StickCam; if none is usable, defer that extraction as well. Experimental visual motion is disabled by default in the guided path; evidence sources can be individually selected in technical tools.
4. **Resolve exceptions.** The guided workspace reports unavailable footage, missing calibration, low observed input coverage, stale human decisions and unmatched clips. Automatic calibration searches visible frames at five-second intervals and proposes shell, gimbal, centre and tip points. Confirm/edit suggestions; hidden tips are not guessed. A confirmed session radio reference assists later clips. Wonky is the default, with Fixed and 180° orientation available. Catalogue shapes provide soft hints only; a selected label never filters a visible radio. Upright/inverted layout suggestions remain editable and ambiguous frames need confirmation.
5. **Review synchronized content.** Open a proposed pair without deriving motion. Play/seek both videos over their common interval, inspect start/end events, adjust the offset and confirm. Audio waveforms are available independently of motion processing. Technical charts show measured telemetry and experimental stick/visual estimates separately. Evidence counts are not probabilities.
6. **Handle split recordings.** Several nonoverlapping FPV segments may belong to one StickCam. A confirmed content alignment can predict other O4 segments using relative filenames and the shared interval. Timestamp predictions remain unverified until individually reviewed. Each FPV file has one confirmed owner; replacing it is explicit.
7. **Export or edit.** Every export needs a current confirmed offset bound to unchanged source signatures. Separate clips share a zero start, export FPS and frame count. Choose original StickCam framing or optional centered/level framing from current saved outline observations. Framing is bounded to source pixels and the whole visible radio; lost or impossible intervals retain original full frames. The 9:16 editor offers the established 60/40 split with static pane transforms. Portrait exports do not bake in the separate stabilization option.
8. **Resume and retain.** Cached extraction survives cancellation and reruns. Last completed results remain available while a new run is pending, failed or cancelled. Draft calibrations are kept on the browser device and bound to source signatures. Session deletion is recoverable and leaves exports and source media intact.

## Automatic vs technical controls

| Normal workflow | Technical tools |
| --- | --- |
| Automatic audio-first matching and exception queue | Select individual sources and run selected evidence together |
| Review next proposed pair | Inspect every candidate, offsets, sections, alternatives and warnings |
| Calibrate only where needed | Manual outline, gimbals, tips, centres, radio identity and orientation |
| Reuse saved data | Extraction Hz, 0.5/0.25 source FPS ratios, comparison Hz and decoder |
| Original or prepared centered StickCam export | ACTUAL / Betaflight / KISS rates, camera uptilt, IMU axes and gyro timing |
| Shared-interval video preview | FPV preparation, motion charts, audio bands and session telemetry scans |

Default extraction/comparison remain 30 Hz, minimum overlap 2 seconds, Wonky StickCam. Audio-first automatic acceptance needs more than the minimum overlap: at least eight seconds of useful audio support and three jointly agreeing independent sections, plus distinctive content and ownership/offset separation. These are conservative heuristics pending real-footage evaluation.

## Correctness rules

- Originals are never modified. Preview fragments use a small, on-demand browser-compatible encoding; matching and exports use originals.
- Disabled evidence sources must not trigger decoding in the matching run. Separate preparation tools operate only when requested.
- Cached storage is not proof of usable tracking. Report joint/per-axis observed coverage, quality gaps and extraction timing.
- Radio tracking anchors against the confirmed reference, checks distributed shell appearance and rejects out-of-frame/drifted outlines. Lost shell observations invalidate stick samples. StickCam gyro can support short, locally agreeing camera-pose gaps; it cannot become pilot-input evidence or resurrect known shell loss.
- Perspective rectification applies consistently to housing coordinates and tips. Both axes are corrected for confirmed 180° orientation; source coordinates remain unchanged.
- Filenames and similar lengths never independently verify a pair. Timestamp estimates never become confirmed alignments automatically.
- Confirmation/rejection records bind both source signatures. Changed media and legacy decisions without signatures require review again.
- Preparation is bounded; calibration editing is reserved while relevant extraction runs. Long batches receive workload-sized limits and retain completed per-clip caches.

## Remaining work, in priority order

### 1. Establish accuracy on labelled real recordings

The known true pair is StickCam `DJI_20260406162534_0228_D.MP4` and FPV `DJI_20260406161138_0008_D.MP4`. The earlier study found repeated distinctive audio evidence around FPV 5.6s / StickCam 0s, while outline tracking drifted onto grass and stick-derived motion did not corroborate the pair. Evidence: [pair study](artifacts/pair-validation/0228-0008/README.md).

The new tracker and staged audio gates have not been run against that footage in this review. Next evaluation should include this pair, several other confirmed pairs and wrong combinations, fixed/wonky/inverted cameras, pinching/thumbing, blur, occlusion, repeated motor audio, unset clocks and O4 splits. Measure false proposals, correct offsets, observation coverage and processing time; do not lower thresholds merely to make a known pair pass.

**Gate:** repeated content support and correct common-start offsets on labelled clips, with ambiguous/wrong combinations staying unconfirmed. Human review remains required until these measurements justify a separate opt-in policy.

### 2. Validate optional stabilization and exports

Exercise native FFmpeg dynamic transforms on representative 1080p/4K, rotated and variable-frame-rate sources. Inspect pixel boundaries, the entire radio, gap transitions, equal counts, audio timing and Resolve import. The source-bound geometry logic cannot prove visibility between sampled observations or recover off-frame/occluded pixels. Abrupt full-frame fallbacks are visible, intentional limits.

**Gate:** no artificial missing-pixel borders, no discarded radio corners, accurate shared timing and correct fallback reporting. Portrait integration of dynamic radio framing remains a separate deliverable.

### 3. Improve unresolved matching science

Prioritize embedded gyro/body-axis validation with rates and mount orientation. Experimental visual fallback now estimates relative 3D rotation with FOV/projection approximations and mount correction. It has not been validated against known gyro and is not a calibrated lens/pose system or gravity-aware dynamics. Inverted freestyle and parallax require the camera/lens/attitude model described in [FREESTYLE_MOTION_DESIGN.md](FREESTYLE_MOTION_DESIGN.md). Throttle/gravity coupling is not reliable matching evidence without vehicle/translation modeling.

**Gate:** quality-qualified rotation correspondence on labelled flights before stronger automated claims. Covered tips remain gaps; a trained detector/reference library needs representative annotated footage, not just catalogue silhouettes.

### 4. Improve operational convenience

Controlled rescan/add/remove recordings in an established session, cross-session named pilot/radio profiles, source fingerprint strengthening where needed, better ETA models from measured workloads, bounded cache inspection/pruning, and durable distributed task scheduling remain follow-ups. Current relink handles existing filenames only; it does not discover new clips or mount an SMB share.

**Gate:** source/analysis revisions and saved decisions remain traceable, originals/exports stay independent, and larger sessions remain responsive.

## Review evidence for this change

Static Python/JavaScript syntax and patch checks plus browser rendering inspection. No new automated tests, footage benchmark, full matching run or export encode was performed for this review. Previous successful data remains available; existing extraction caches are retained but incompatible tracker versions require fresh derivation when requested.

### Explicit controls and visual rates

Radio Modes 1–4 now map physical observations to Throttle/Yaw/Pitch/Roll, defaulting to Mode 2 and reusing tracking when assignments change. The 180° view control updates draft extraction orientation too. FPV visual preparation uses camera rotation-vector increments, FOV/projection-specific cache identities and mount correction. The acceptance gate remains a labelled known-gyro comparison, especially inversion, parallax, occlusion and stabilized/cropped recordings. No automatic or frame-accurate claim is made before that evaluation.
