# Cross-modal reference analysis — 15 October 2026

## Most useful method

Overall StickCam scene activity after coarse camera-motion compensation, compared with angular-rate changes from O4 embedded attitude. Non-overlapping 3-second windows vote for one common lag. The reference offset is not supplied to the search.

On these five already-known pairs, all five saved offsets were recovered within 0.10 seconds. Four meet the conservative repeated-section gates; the approximately 15-second O4 clip has only three agreeing windows and remains a manual review case. This is development-cohort offset recovery, not blind identification among unrelated files.

| StickCam | FPV | Saved offset | Found offset | Error | Agreeing 3s windows |
|---|---|---:|---:|---:|---:|
| DJI_20261011114753_0018_D.MP4 | DJI_20261011110859_0002_D.MP4 | 19.999202s | 20.000s | +0.0008s | 3/5 |
| DJI_20261011153226_0037_D.MP4 | DJI_20261011145311_0010_D.MP4 | 2.713344s | 2.700s | -0.0133s | 20/55 |
| DJI_20261011154217_0039_D.MP4 | DJI_20261011150315_0015_D.MP4 | 14.200000s | 14.200s | +0.0000s | 10/24 |
| DJI_20261011154823_0040_D.MP4 | DJI_20261011150916_0016_D.MP4 | 10.700000s | 10.800s | +0.1000s | 14/23 |
| DJI_20261011160849_0042_D.MP4 | DJI_20261011152941_0020_D.MP4 | 8.600000s | 8.700s | +0.1000s | 12/28 |

## Extraction and reuse

- First successful full-length source extraction/study pass: **175.49 seconds**. Generic scene and audio features are cached independently of stick calibration.
- Latest repeated-section and cross-modal analysis using saved source data: **9.85 seconds** for all five pairs.
- Five StickCam scene caches cover about 97–100% of samples; this measures usable generic image-change observations, not correctly identified controls.

## What helped and what did not

The original full-trace scene correlation found three reference offsets; repeated short-section voting found all five. Saved gimbal activity also recovered useful timing in two pairs. Naively averaging visual and audio channels produced wrong offsets for all five, so the combined search now uses timing consensus and separate source contributions.

The combined search found four of the five reference offsets. For the short 0018/0002 pair, audio suggests a different event; this is marked as a modality conflict and fails eligibility. The independent overall-scene method still recovers its saved timing, with insufficient evidence for an automatic suggestion. Motor-band audio has only about 4–21% usable prepared coverage in these clips, and does not independently recover the known timing. It remains an optional experimental fallback.

## Tracking diagnosis and next improvements

Two cached precise-input examples retain a radio outline for only **23.5%** and **32.7%** of the clip; detected control coverage is **0–1.9%**. Generic scene activity remains available across most of the footage. The first issue to address for precise tracking is radio pose reacquisition, followed by tip occlusion/ambiguity.

1. Review the automatically proposed face outline and housing boxes before deriving precise tips. Keep radio-model silhouettes as soft hints.
2. Capture several clear face references across different viewing angles, grip positions and 0/180-degree orientations, using full-frame tracking landmarks. Reacquire the immutable radio face from these references and require distributed face features. A tip-only mark cannot recover a lost pose.
3. Evaluate gimbal housing tracking separately from tip tracking. Preserve explicit intervals where the radio or tip is hidden.
4. Use saved manual corrections as a labelled evaluation set for future detector changes, with separate outline/housing/tip metrics and unseen clips.
5. Consider hand/knuckle motion, motor-tone dynamics, takeoff/landing events and motion blur as additional timing cues. Treat these as activity evidence rather than inventing signed stick positions or gravity measurements.

## Where to review

Open **Movement rhythms & cross-modal evidence**, choose the saved confirmed-pair study ending in **225fb9d65a5d664b1b31**, and open each pair. Select individual channels, inspect alternative timing peaks, seek the synced videos, or download the plotted data. **Overall movement** availability appears in **Video lists & match scope**, and **Motion comparison** has an independent scene-movement row.

Full numeric results and coverage/recommendations are in [reference-results.json](reference-results.json). Saved traces remain in the session and source-bound cache. Existing confirmed pairs and exports were retained.
