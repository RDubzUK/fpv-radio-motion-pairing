# Code and workflow review — 3 October 2026

## Changes and reasons

The initial implementation forced global assignments even with weak scores, compared sample indices as though camera rates were identical, used a dark-pixel median that could follow gimbal housing, and kept web jobs only in memory. Whole-batch multipart uploads, full-resolution StickCam processing and unconstrained media encoding made large batches unnecessarily costly.

The review replaced those mechanisms with timestamp-aware sampling, selected-tip tracking, independent gimbal quality, unmatched assignment options, durable jobs, local imports, streaming uploads and bounded media processes. It added per-clip calibration, tracking contact sheets, saved pair/offset corrections, linked overlap review, fingerprint-based audio evidence and relative camera-clock handling.

Exports now use equal frame counts and can use an editing codec with PCM audio. ZIP downloads stream without storing a second copy of the exported media. Cancellation and rerun status are scoped to an individual run so an older cancelled worker cannot overwrite a newer run.

The original project was backed up before editing at:

`/home/rdubz/vscode_projects/.project-backups/fpv-radio-motion-pairing-before-review-20261003.tar.gz`

## Functional review evidence

The review used generated data in an isolated temporary workspace; no real user recordings were supplied or analysed.

| Check | Observed result |
| --- | --- |
| Different signal rates, sign inversion and an invalid section | Known +3.270s offset recovered at +3.271s |
| Later calibration reference and negative offset | Known −4.000s offset recovered at approximately −3.996s |
| Generated fixed StickCam and textured FPV video, 30/25 fps | Known +4.000s offset recovered at approximately +4.001s |
| Generated moving transmitter/camera | Known +4.000s offset recovered at approximately +4.001s |
| Boundary audio with different gain and noisy middle | +4.000s offset recovered; unrelated audio had weak evidence |
| 40 × 40 five-minute synthetic motion fingerprints | All 40 intended pairs selected; about 16s comparison time, 334 MiB peak RSS |
| Browser review | Session restore, upload, pointer calibration, shared playback, frame nudge, saved correction and export links worked without browser exceptions |
| Export timing | Reviewed example produced two 718-frame, 30 fps clips, each 23.933333s |
| ZIP stream | Readable archive and successful CRC validation |
| HTTP media | Seek ranges returned 206 with requested byte counts; HEAD returned headers without body |
| Radio-body orientation | Synthetic 9° radio rotation estimated at approximately 9°; body compensation reduced apparent static-stick drift from 0.375 to 0.012 |
| Betaflight rate curves | Synthetic ACTUAL and legacy curves were finite, monotonic and symmetric; invalid profile values are rejected |
| Static review | Python compilation, JavaScript syntax and project Ruff checks passed |

The comparison benchmark excludes decoding and motion extraction. Synthetic recordings do not validate matching accuracy on real FPV footage. Browser playback is approximate; export mechanics retain the chosen timing estimate but cannot correct a wrong estimate.

## Operational state

The project uses an isolated Python 3.11 environment and a locked dependency set. The user service `fpv-radio-motion-pairing.service` runs the viewer on all network interfaces at port 8767, independently of this session.

The normal data directory is `~/.fpv-radio-motion-pairing`. Temporary review media is separate from normal sessions. Original local media is read in place. Completed fingerprints and exports remain reusable.

## Practical limits

- Real stick-to-motion correlation and thresholds still require labelled flight recordings.
- Radio-outline correction depends on visible textured shell features; low tracking quality falls back to the existing gimbal-local tracking. Severe perspective changes are outside the affine model.
- Optional Betaflight profiles apply to all FPV clips in one run. They can represent per-axis expo/curve shape, while correlation largely removes a uniform overall rate multiplier.
- Selected-tip and housing tracking can fail under occlusion, fast jumps or large perspective changes. The tracking check and quality plots expose that uncertainty.
- Only a single consistent camera-clock relationship is modelled per session. Reset clocks or date changes can remove filename evidence.
- Audio requires shared audible events; many flights will have little useful cross-camera sound.
- Processing cost depends on recording duration, codec and CPU decoding speed. The application bounds resource use; the benchmark is not a promise for a complete 4K batch.
- Compact export is 8-bit H.264/AAC. DNxHR HQX/PCM offers an edit-oriented alternative with much larger files.
- A clip can contain more than one flight, but current assignment is one-to-one between recordings rather than automatic multi-flight segmentation.
