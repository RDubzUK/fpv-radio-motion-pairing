# Workflow review — 4 October 2026

## Finding

The project accumulated useful technical tools but the main path required heavy experimental processing too early. A reliable product needs cheap content screening, honest failure states, quick synchronized review, durable decisions and safe exports. The implemented path is documented in [PROJECT_PLAN.md](PROJECT_PLAN.md).

## Corrections delivered

| Problem | Correction |
| --- | --- |
| Motion extracted before audio; uncalibrated clips lost even for audio | Staged automatic matcher scans cached boundary audio first, keeps unconfigured sources and escalates unresolved motion only |
| Unavailable selected FPV motion could still trigger full StickCam extraction | Automatic fallback checks FPV motion before deriving StickCam and defers inputs when there is no usable comparison |
| A high tracking-quality value could follow grass/background | Immutable reference-coordinate homographies, distributed shell checks, bounded outlines and confirmed-face reacquisition |
| Hidden/ambiguous tips replaced by plausible dark housing blobs | Distinctive tip evidence and calibrated travel gates; missing observations remain gaps |
| Cached movement implied usable movement | Joint/per-axis coverage, usable seconds and low-coverage warnings |
| All major technical sections demanded attention immediately | Guided workspace with next actions, review queue and technical tools on demand |
| Audio/video inspection could initiate full visual extraction | Metadata-only synchronized preview and standalone audio-waveform review |
| Same FPV filename replaced under an old confirmed offset | Decisions bind both source signatures; changed and signature-less legacy decisions require review again |
| Ordinary aligned exports accepted unreviewed candidates | All exports require current confirmed alignment |
| Reject then Export silently reconfirmed a pair | Reject clears local reviewed state; export cannot confirm implicitly |
| Another StickCam silently displaced a confirmed FPV owner | Explicit replacement intent and atomic owner guard |
| Selected FPV early matching consumed its StickCam | Reuse StickCam across split FPV files; preserve one owner per FPV segment |
| Cancelled/failed rerun hid previous successful results | Last completed results remain accessible independently of current run status |
| Two-hour timeout covered an entire large batch | Workload-sized bounded task limit, with completed per-clip caches retained |
| Source folders vanished after setup | Collapsed source availability and controlled existing-file relink |
| Fast folder navigation selected stale asynchronous content | Request token and disabled selection while loading |
| Unsaved calibration disappeared on session switch/reload | Local drafts bound to source signatures, visible dirty state and discard |
| Stabilized export requested but absent | Optional separate StickCam export from current outline cache, bounded native crop/rotation and original-frame fallback |
| Wrapper tasks could reuse pre-correction tracking results | Current tracker/outline versions included in comparison task fingerprints |

## Known limits and next work

The 0228/0008 labelled-pair investigation supplies prior audio evidence and a demonstrated tracking failure; it is not an evaluation of the new implementation. Targeted calibration frames and playback against saved visual estimates were inspected. No complete matching run or export encode was performed. Conservative audio gates and tracker checks need measured true/false-pair and timing evaluation. Scores are not calibrated probabilities; confirmation remains human.

Experimental 2D FPV flow does not supply complete inverted-flight attitude, gravity or throttle dynamics. Embedded telemetry remains preferred. Radio catalog outlines are shape proposals, not a trained identity model. Severe hand occlusion/viewpoint changes can still create gaps.

Centered export does not recover missing radio pixels, rectify perspective or prove visibility between sampled observations. It intentionally falls back to original full frames. Native dynamic FFmpeg transforms and final Resolve timing need evaluation. Portrait exports currently use static pane framing; dynamic stabilization is separate.

Source relink handles existing session names, not session rescan/new footage or SMB mounting. Browser-local drafts are not server-shared. Source signatures use size/mtime (verified upload identities strengthen cache reuse); cryptographic identity for mutable local media is a future enhancement. Current single-host bounded worker queues are suitable for the intended workload but are not a distributed scheduler.

## Review record

Python/JavaScript syntax checks, patch review and browser rendering inspection were performed. No new automated tests or benchmarks were added/run. Prior sessions, source media, legacy cache files and exports were retained. Complete matching and native export evaluation are explicit next acceptance gates in the project plan.

## Gimbal suggestion regression

The frontend now loads calibration frames on demand and exposes busy/error feedback beside Suggest. Model labels are soft hints across all body families. The TX16S layout includes Mk1, square-body proportions and upper-half gimbals; orientation compares body and display placement, with ambiguity explicit. Single-frame reproduction exposed background-circle false proposals and hand-covered rims. Scale-separated detection and joint body/rim ranking address those causes, but cluttered frames still require a marked outline or confirmed photographic reference. These heuristics do not constitute validated model recognition.

## Stick assignments and visual rates correction

Mode 2 was hard-coded but semantic channels were already stored. Modes 1–4 are now explicit and saved, with cheap remapping of cached physical/canonical observations. A 180° calibration view now changes draft orientation and physical gimbal assignment together. Visual extraction now estimates relative camera SO(3) rotation using explicit FOV/projection approximations, with low-parallax rotation fits or guarded essential pose. Review and matching share body Roll/Pitch/Yaw rates with mount correction. Old 2D caches remain on disk and are not relabelled as gyro. Syntax/import checks and the inverted marked-outline reproduction were performed; the new visual estimator has not yet been evaluated on real known-gyro footage.
