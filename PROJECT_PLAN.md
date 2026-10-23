# FPV / StickCam: complete workflow plan

Updated: 13 October 2026. Current review: [WORKFLOW_REVIEW.md](WORKFLOW_REVIEW.md). Earlier development and the known-pair investigation: [PROJECT_HISTORY.md](PROJECT_HISTORY.md).

## Intended result

The user-facing product is **PropNutz Flight Matcher**, an associated tool for the PropNutz FPV community. Branding follows https://www.propnutz.co.uk/: charcoal panels, warm off-white text, red primary actions, blue accents and Barlow/Barlow Condensed typography. Assets are local for offline/LAN and packaged use; calibration and matching workflows retain their existing controls.

A saved session turns a folder of FPV recordings and a folder of StickCam recordings into content-supported pairing proposals, synchronized review, and aligned exports or a portrait edit. The normal workload is 10–40 flights, often 1080p–4K, with unequal recording lengths and potentially several O4 FPV files for one StickCam. The default path should require only source selection, exception handling and a short alignment review. Technical controls remain available when useful.

“No touch” means avoid unnecessary calibration and repeated decoding, retain progress and explain exceptions. It does not mean automatically approving uncertain pairs. A safe unmatched result is preferable to an incorrect aligned export.

## The process now implemented

1. **Name the session and choose sources.** Browse server folders, including mounted SMB shares, or upload resumably. Keep sources read in place. A collapsed video list supports all, some or one target from either side. A source summary allows availability checks and relinking existing filenames after a drive/share moves.
2. **Find flights automatically.** Read metadata and saved boundary-audio fingerprints first. Check distinct spectral landmarks, independently agreeing audio trends, repeated 3-second sections, competing offsets and competing StickCam owners. Distinctive proposals skip unnecessary vision. Audio identifies content; timestamps establish relative camera clocks only after a content anchor. Dates can be wrong or reset to 1970. Duration only defines possible overlap.
3. **Prepare unresolved evidence.** The lighter movement preset reuses cached, tip-free gimbal activity and needs the radio outline/regions rather than precise tips. Full StickCam inputs remain separately selectable. Use embedded FPV gyro/attitude before any visual fallback. Unconfigured or invalid sources remain in the results with a reason. Without trusted StickCam calibration, complete audio review and defer expensive FPV vision. Check selected FPV motion availability before deriving StickCam; if none is usable, defer that extraction as well. The movement presets enable experimental visual fallback for recordings without telemetry; it can be disabled to avoid that extraction. Evidence sources remain individually selectable.
4. **Resolve exceptions.** The guided workspace reports unavailable footage, missing calibration, low observed input coverage, stale human decisions and unmatched clips. Automatic calibration searches visible frames at five-second intervals and proposes shell, gimbal, centre and tip points. Confirm/edit suggestions; hidden tips are not guessed. A confirmed session radio reference assists later clips. Wonky is the default, with Fixed and 180° orientation available. Catalogue shapes provide soft hints only; a selected label never filters a visible radio. Upright/inverted layout suggestions remain editable and ambiguous frames need confirmation.
5. **Review synchronized content.** Open a proposed pair without deriving motion. Play/seek both videos over their common interval, inspect start/end events, adjust the offset and confirm. Audio waveforms are available independently of motion processing. Technical charts show measured telemetry and experimental stick/visual estimates separately. Evidence counts are not probabilities.
6. **Handle split recordings.** Several nonoverlapping FPV segments may belong to one StickCam. A confirmed content alignment can predict other O4 segments using relative filenames and the shared interval. Timestamp predictions remain unverified until individually reviewed. Each FPV file has one confirmed owner; replacing it is explicit.
7. **Export or edit.** Every export needs a current confirmed offset bound to unchanged source signatures. Separate clips share a zero start, export FPS and frame count. Choose original StickCam framing or optional centered/level framing from current saved outline observations. Framing is bounded to source pixels and the whole visible radio; lost or impossible intervals retain original full frames. The 9:16 editor offers the established 60/40 split with static pane transforms. Portrait exports do not bake in the separate stabilization option.
8. **Resume and retain.** Cached extraction survives cancellation and reruns. Last completed results remain available while a new run is pending, failed or cancelled. Draft calibrations are kept on the browser device and bound to source signatures. Session deletion is recoverable and leaves exports and source media intact.

## Automatic vs technical controls

The dedicated **Find StickCam counterpart** section selects one FPV recording and searches the full non-excluded StickCam list at 100% coverage, independent of the general sample controls. It offers audio-first Quick Match with embedded gyro/calibrated-input fallback, an explicit experimental visual-motion option, audio-only matching without calibration, or the existing selected-evidence configuration. It reuses caches and the standard matching/result/confirmed-preview pipeline. Progress, cancellation, ranked candidates, section evidence, offsets/overlaps and missing preparation are displayed in the section. Previous completed candidates remain available and sampled coverage is labelled. The user previews and confirms through the shared interval viewer; finding a candidate never approves a pair or an export automatically.

| Normal workflow | Technical tools |
| --- | --- |
| Automatic audio-first matching and exception queue | Select individual sources and run selected evidence together |
| Review next proposed pair | Inspect every candidate, offsets, sections, alternatives and warnings |
| Calibrate only where needed | Manual outline, gimbals, tips, centres, radio identity and orientation |
| Reuse saved data | Extraction Hz, 0.5/0.25 source FPS ratios, comparison Hz and decoder |
| Original or prepared centered StickCam export | ACTUAL / Betaflight / KISS rates, camera uptilt, IMU axes and gyro timing |
| Shared-interval video preview | FPV preparation, motion charts, audio bands and session telemetry scans |

Default extraction/comparison remain 30 Hz, minimum overlap 2 seconds, Wonky StickCam. Audio-first automatic acceptance needs more than the minimum overlap: at least eight seconds of useful audio support and three jointly agreeing independent sections, plus distinctive content and ownership/offset separation. These are conservative heuristics pending real-footage evaluation.

## Quick Match implementation plan

Quick Match is the fast, staged path for proposing flight pairs. It starts with inexpensive file/stream inspection and cached evidence, tries audio first, and performs motion extraction only for clips that remain unresolved. It proposes relationships and common ranges for review; it never silently confirms a pair from timestamps, durations or a single weak correlation.

The guided first-pass scope can sample 10%, 25%, 50%, or all selected files. Samples are spread across each filename-time/file-date-ordered list, with at least one file from each camera. After a distinctive content anchor establishes a tentative camera-clock difference, remaining clips can appear as separate clock-and-duration preview suggestions; these are explicitly unscanned and never get a content score or automatic confirmation. The user can run all files afterward to verify them with audio or motion.

### 1. Scan and summarize both file lists

Scan media metadata without decoding full-resolution frames. For every clip, record duration, file modified time, available audio streams, supported FPV embedded gyro/attitude, cached or derivable FPV visual motion, StickCam calibration state, and cached StickCam input data. Keep these checks bound to the source signature so a changed file cannot inherit stale availability or cached evidence.

The first pass now shows a compact availability status beside each filename in the FPV and StickCam lists. The FPV list shows **Audio**, **Embedded gyro**, **Visual motion**, **Filename time**, **Modified time**, and **Duration**. The StickCam list shows **Audio**, **Calibration**, **Derived inputs**, both date clues, and duration. Audio stream presence is distinguished from useful fingerprint cache status; gyro that has not been checked remains unknown. Invalid/default dates such as 1970 are marked suspect. Availability and caches remain tied to the current source signature.

### 2. Match available audio first

For clips with audio on both sides, reuse cached fingerprints or inspect useful boundary sections near the start and end first. Search for distinctive, agreeing sections and retain each section’s two clip-relative times, duration, and quality. The motor noise and unequal recording lengths mean whole-clip audio similarity is not a requirement. Audio evidence should be tested across competing candidates and owners; only confident content evidence creates a seed match.

If audio is absent, continue directly to motion. If audio exists but cannot establish a confident match, run the motion fallback only for the unresolved clips. Preserve any confident audio matches while continuing on the remaining files.

### 3. Expand verified matches using timestamps and durations

For every seed match, record both the content-derived clip alignment and the difference between filename or modified-time clocks. Check whether the timestamp difference agrees across independent matches before treating it as a session-level offset. Modified-time expansion defaults to recording completion and subtracts duration to estimate its start; a saved start/end choice and ±5s tolerance are available. Independently confirmed pairs refresh session-wide proposals immediately, while unverified date/rhythm confirmations cannot teach anchors; filename clocks remain start-time clues. A single anchor is tentative. Treat absent, reset, or inconsistent clocks as unavailable for expansion.

Once a clock offset is supported, combine it with each clip’s duration to map possible overlap intervals. Use those intervals to suggest one-to-one pairs and one-to-many groups, including consecutive O4 recordings that cover one StickCam flight. Duration and timestamps narrow candidates only after a content match has anchored the offset; neither is standalone proof. Keep the original per-file start offsets so the user can see the actual common start points.

### 4. Use motion for the remaining clips

For each unresolved FPV clip, use cached embedded gyro/attitude when supported. If it is absent, derive visual angular motion and save it against the source signature for reuse. For each unresolved StickCam clip, reuse valid calibration and saved inputs. If calibration is missing or uncertain, run the existing calibration suggestions and send only those clips to the calibration review step before deriving inputs.

Compare FPV roll, pitch and yaw rates with expected rate-command signals calculated from calibrated StickCam axes and the saved rates profile. Search possible time shifts using repeated short sections, per-axis agreement and competing-candidate separation; do not require equal durations. Treat embedded gyro as stronger evidence than image-derived motion, and mark visual estimates as experimental. Stick throttle is a separate signal and must not be treated as a direct gyro axis. Cache every completed derivation so later Quick Match runs skip that work.

### 5. Rank proposals and ask for review

For each proposed relationship, show which evidence agreed, how many independent sections supported it, the time offset, modified-time difference, inferred common interval, source availability, and any weak or missing signals. Show one-to-many FPV groups together. Let the user accept, reject, adjust alignment, or continue scanning. Keep low-confidence and clock-only candidates as suggestions, and report why clips remain unmatched instead of forcing a pairing.

**Acceptance gates:** file-list statuses accurately distinguish present, prepared, missing and failed evidence; absent audio reaches motion fallback; audio-found-but-inconclusive cases also reach fallback for unresolved clips; embedded gyro is preferred over derived visual motion; cached evidence is reused; a timestamp offset is used for split-file expansion only after content matches support it; unequal durations do not suppress real pairs; and no candidate becomes confirmed without user review.

### GPU and live extraction updates

Tracking now automatically uses a compatible OpenCV CUDA runtime for LK flow, perspective crops and tip template matching, with an explicit CPU fallback. The RTX 3090 host has a locally built official CUDA-enabled OpenCV 4.14.0 environment; its current StickCam batch reports actual CUDA operations. SIFT/geometry/confidence validation remains on CPU, and reduced frames still cross host/device memory. Routine radio SIFT reacquisition is restricted to a generous region around the last accepted outline, with periodic full-frame searches and immediate full-frame fallback for invalid/large regions; appearance, geometry and confidence gates are unchanged. Per-video diagnostics now accumulate decode, radio shell, gimbal, flow, crop, template, upload and SIFT/match/validation times and local/global search counts. They are surfaced during a run and in the derivation log so CPU bottlenecks can be identified. No speed-up benchmark was run; stage times are diagnostic and some subtotals are included within the shell/gimbal totals. Backend choice does not invalidate existing StickCam caches; timing history distinguishes CUDA from CPU runs.

New StickCam derivations publish a bounded 20-second live preview of semantic inputs/quality through the existing progress file. It uses the selected Mode, preserves observation gaps and resets between disconnected forward/backward spans. Current workers finish with their existing code. The final deployment waits for extraction/exports to become idle before restarting the viewer.

### StickCam tracking review and labelled references

The **Calibrate & review StickCam** workspace pairs playback with saved normalized Throttle/Yaw/Pitch/Roll traces and tracked source-pixel radio outline, gimbal polygons and tips. Native-rate overlay windows avoid overview downsampling on long clips. Older caches expose labelled geometry reconstruction where possible, and can be re-derived individually for full geometry.

Users can mark left/right tips or a complete frame calibration over time. Tip landmarks refresh visible tip appearance in a reliable pose; full frame landmarks reset radio/gimbal references at the marked time. Off-grid landmark frames are decoded explicitly in either tracking direction. Source-bound landmarks enter the calibration/cache identity and are reused in matching after re-derivation. Ordinary quality gates remain in force between corrections, and a tip mark cannot restore a lost shell pose. Semantic expected controls and notes remain diagnostic labels, not matching evidence. Labels can also be collected without a usable derivation.

The review retains the pre-correction cache baseline, reports original/current pixel disagreements and tracking rejection reasons, and exports the labels, source/calibration signatures and bounded trace in JSON. These examples support targeted evaluation of appearance, housing drift and hand occlusion. Synthetic checks cover template recovery, frame-pose reset, both tracking directions, inverted stick assignments, cache reuse and stale/active edit guards. Real-flight evaluation and a reusable detector trained on representative corrected examples remain outstanding; local corrections do not train a general model automatically.

Visual motion v5 uses distributed features, native-rate intermediate tracking (optional, capped at 120 Hz), descriptor reacquisition and composed rotation increments. Saved output Hz remains independent. Gaps record rejection reasons. Optional short-gap interpolation is dashed and used only for viewing; it never becomes matching support or modifies the source cache. Older visual caches are reviewable and marked for upgrade. Real-footage/known-gyro evaluation remains the accuracy gate.

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

### 1. Evaluate Quick Match on labelled footage

The scan/status badges, audio-first path, unresolved-only motion fallback, cached evidence reuse, sample percentages, tentative clock-based suggestions and full-file follow-up are implemented. Validate the status display and timing expansion on missing audio, missing gyro, uncalibrated StickCam, stale caches, invalid dates, changed/unreadable files and real O4 splits. In particular, compare filename and modified-time clock assumptions on labelled recordings.

**Gate:** the known audio-confirmed pair is proposed quickly, an audio-less or audio-inconclusive case reaches motion, O4 fragments are suggested only after a content anchor, and wrong/ambiguous candidates remain unconfirmed.

### 2. Establish accuracy on labelled real recordings

The known true pair is StickCam `DJI_20260406162534_0228_D.MP4` and FPV `DJI_20260406161138_0008_D.MP4`. The earlier study found repeated distinctive audio evidence around FPV 5.6s / StickCam 0s, while outline tracking drifted onto grass and stick-derived motion did not corroborate the pair. Use the tracking review labels on this and other difficult clips to compare expected stick tips/controls with the saved automatic trace. Evidence: [pair study](artifacts/pair-validation/0228-0008/README.md).

The new tracker and staged audio gates have not been run against that footage in this review. Next evaluation should include this pair, several other confirmed pairs and wrong combinations, fixed/wonky/inverted cameras, pinching/thumbing, blur, occlusion, repeated motor audio, unset clocks and O4 splits. Measure false proposals, correct offsets, observation coverage and processing time; do not lower thresholds merely to make a known pair pass.

**Gate:** repeated content support and correct common-start offsets on labelled clips, with ambiguous/wrong combinations staying unconfirmed. Human review remains required until these measurements justify a separate opt-in policy.

### 3. Validate optional stabilization and exports

Exercise native FFmpeg dynamic transforms on representative 1080p/4K, rotated and variable-frame-rate sources. Inspect pixel boundaries, the entire radio, gap transitions, equal counts, audio timing and Resolve import. The source-bound geometry logic cannot prove visibility between sampled observations or recover off-frame/occluded pixels. Abrupt full-frame fallbacks are visible, intentional limits.

**Gate:** no artificial missing-pixel borders, no discarded radio corners, accurate shared timing and correct fallback reporting. Portrait integration of dynamic radio framing remains a separate deliverable.

### 4. Improve unresolved matching science

Prioritize embedded gyro/body-axis validation with rates and mount orientation. Experimental visual fallback now estimates relative 3D rotation with FOV/projection approximations and mount correction. It has not been validated against known gyro and is not a calibrated lens/pose system or gravity-aware dynamics. Inverted freestyle and parallax require the camera/lens/attitude model described in [FREESTYLE_MOTION_DESIGN.md](FREESTYLE_MOTION_DESIGN.md). Throttle/gravity coupling is not reliable matching evidence without vehicle/translation modeling.

**Gate:** quality-qualified rotation correspondence on labelled flights before stronger automated claims. Covered tips remain gaps; a trained detector/reference library needs representative annotated footage, not just catalogue silhouettes.

### 5. Improve operational convenience

Controlled rescan/add/remove recordings in an established session, cross-session named pilot/radio profiles, source fingerprint strengthening where needed, better ETA models from measured workloads, bounded cache inspection/pruning, and durable distributed task scheduling remain follow-ups. Current relink handles existing filenames only; it does not discover new clips or mount an SMB share.

**Gate:** source/analysis revisions and saved decisions remain traceable, originals/exports stay independent, and larger sessions remain responsive.

## Review evidence for this change

Static Python/JavaScript syntax and patch checks plus browser rendering inspection. No new automated tests, footage benchmark, full matching run or export encode was performed for this review. Previous successful data remains available; existing extraction caches are retained but incompatible tracker versions require fresh derivation when requested.

### Explicit controls and visual rates

Radio Modes 1–4 now map physical observations to Throttle/Yaw/Pitch/Roll, defaulting to Mode 2 and reusing tracking when assignments change. The 180° view control updates draft extraction orientation too. FPV visual preparation uses camera rotation-vector increments, FOV/projection-specific cache identities and mount correction. The acceptance gate remains a labelled known-gyro comparison, especially inversion, parallax, occlusion and stabilized/cropped recordings. No automatic or frame-accurate claim is made before that evaluation.

## Sister-project parity and rhythm review — 8 October 2026

The [parity audit](docs/SISTER_PROJECT_PARITY.md) records the relevant Flight Sync changes now incorporated, their isolated checks and remaining platform/real-footage acceptance. Confirmed modified-date expansion, independent verification gates, adaptive cached audio, split-flight review/conflict checks, native/resumable exports, projects/backups and startup diagnostics now form the same complete review process in this motion-capable app.

The additional movement-rhythm option supplies an unsigned shortlist from saved observations, with common-offset section evidence and competing-candidate gates. It does not depend on correct axis labels or signs, and it does not enter automated clock/assignment evidence. Next acceptance: labelled real footage where directional motion disagrees, compared against intentionally wrong/repetitive flights and Wonky StickCam camera movement. Measure shortlist recall, false candidates, offset error and runtime; preserve radio-pose gaps. Only an independently reviewed alignment should seed date expansion.


## Unified StickCam workspace — 12 October 2026

Merged the separate calibration/review players into a single viewport with common source-coordinate marking tools, playback/time navigation, trace, corrections and re-derivation controls. Saved tracking loads on clip selection without starting extraction. Starting calibration and timed frame/tip landmarks have explicit editing states and separate save semantics. Playback corrections retain the starting calibration; visible stored poses may seed editable drafts, while absent poses stay unmarked. Advanced diagnostic labels and correction history remain collapsible. Browser review is limited to layout and saved-data behavior; no new tracking accuracy or runtime claim is made by this UI change.


## Manual first-pair discovery and recorder clocks — 13 October 2026

Added an independent two-feed manual browser available immediately after session setup. It calculates an offset from marked visual events and opens normal synchronized review without requiring audio, telemetry, calibration or completed matching. Explicitly confirmed manual alignments are durable, source-bound camera-clock seeds, retain checkpoints, and feed the existing date suggestions and export/editor flow.

FPV drone/recorder labels can be assigned per file or to an explicitly selected FPV subset. Each recorder learns its own clock; incompatible unassigned offsets remain separate tentative alternatives rather than corrupting a single global model. Assigned files only use their recorder's clock; unassigned files can have competing proposals. Split recordings retain individual offsets and common intervals. Camera clock resets need a new label. UI and static review do not establish real-session manual match accuracy or cross-platform playback support.

The manual workspace also has Play both/Pause both at the marked event controls. It plays from the two source marks, holds both feeds during buffering, corrects preview drift and stops at the end of their common remaining footage. Pausing/resuming stays in the manual browser; it never confirms a pairing.
## Implemented: tip-free movement matching (13 October 2026)

- Independent, cached gimbal/finger activity from validated radio poses, up to
  10 Hz; no exact tip or rate-profile requirement.
- Audio-first Quick Match and counterpart integration, selected-source preset,
  embedded rotation preference and experimental visual fallback for DVR footage.
- Radio-region-only saving, cached-data indicators, progress/ETA and per-cache
  extraction duration; full-frame landmarks provide appearance references.
- Content-only timing and repeated-section evidence, explicit competing owners,
  review-only proposals and source-bound independent confirmation before clocks.
- Field validation across more pilots, radios, grips and recording types remains
  open. See `docs/GIMBAL_ACTIVITY_MATCHING.md` and the four-pair study.

## Implemented: sequential O4 movement seeds (14 October 2026)

- Cache supported embedded gyro/attitude across the selected FPV pool.
- Automatically locate radio/gimbal regions in one StickCam at a time, reuse
  compatible activity caches, and skip insufficient clips until a distinct seed.
- Stop after the seed plus 0–3 optional other-flight movement checks; do not
  derive every StickCam as the initial step.
- Suggest related pairs and O4 parts using provisional modified-date clock
  offsets and actual timeline intersections. Keep recorder clock groups separate.
- Save automatically observed regions as reviewable drafts. Keep provisional
  predictions separate from human-confirmed clocks; require independent content
  review before confirmed alignment/export.
- Show preparation counts, observed seconds, skipped clips, per-clip progress,
  cache reuse, clock groups and spot-check outcomes. See `docs/O4_QUICK_MATCH.md`.
- Static/UI checks establish wiring only. End-to-end automatic region success,
  seed precision and real-session timing remain to be measured; the previous
  benchmark used pre-marked regions and does not validate this new workflow.


## Implemented 15 October 2026 — recording scope and cross-modal evidence

- Reversible per-session Ignore/Restore for FPV and StickCam; preserve sources,
  caches, calibrations, confirmations and exports, and filter processing/results.
- Separate possible recorder groups when independently confirmed clock deltas
  differ by more than five minutes; unassigned clips remain candidates in every
  compatible group. Source-bound pair rejections and offset-specific clock
  rejections inform subsequent date suggestions without pooling drone clocks.
- Saved generic scene movement and motor-band audio dynamics, cached by source
  and extraction settings. Display overall movement availability in file lists
  and plot it independently beside existing motion/audio data.
- Read-only views of the exact cached movement/rhythm features, full timeline,
  gaps, local matching bands, playback cursor, per-channel contributions and
  downloadable data. Opt-in motor-audio and combined-rhythm counterpart searches.
- Extract reference signals from independently confirmed pairs; compare audio
  against motion as well as visual against motion. Repeated 3s sections vote for
  timing; correlated channels are not independent votes. No weights are fitted
  to known offsets and no candidate automatically confirms a pair.
- Five current confirmed pairs provided reference material. Generic scene
  movement versus embedded O4 rate changes recovered all five saved offsets
  within 0.10s with repeated-window search. This is same-pair offset recovery,
  not a blind file-identification accuracy result. Naive feature averaging was
  worse and was replaced with timing consensus. Motor audio remains exploratory.
- See `docs/SESSION_SCOPE_AND_RHYTHMS.md` and the reference report under
  `artifacts/pair-validation/cross-modal-20261015/`.
