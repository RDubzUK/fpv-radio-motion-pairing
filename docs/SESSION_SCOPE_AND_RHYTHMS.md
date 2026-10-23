# Session scope and cross-modal rhythm review

## Ignore recordings

In **Video lists & match scope**, each included FPV and StickCam recording has
an **Ignore** action. Ignored recordings move to a separate collapsible list
with **Restore** actions. This setting belongs only to this session and is saved
in each recording's metadata, including portable project documents.

Ignored files are excluded from matching inputs, calibration scans, movement
preparation, manual counterpart selectors, thumbnails and date suggestions.
Their saved confirmations cannot teach a clock while ignored. Sources,
calibrations, cached fingerprints, saved decisions and exports remain on disk.
Restoring makes existing source-current data available again. Finish active
work before changing scope; processing uses a consistent set of included files.

## Several drone clocks and negative evidence

Independent, source-current confirmed alignments teach camera-clock differences.
Modified dates use the configured recording-start/end interpretation. Durations
convert end timestamps to recording starts and determine overlap; they never
establish flight identity.

Confirmed offsets separated by more than **300 seconds** form separate possible
recorder groups, including when files share a manual recorder label. A camera
clock reset can create the same pattern. Smaller disagreements exceeding the
chosen 2–10 second alignment tolerance remain conflicts rather than being
silently averaged into a usable clock. Filename clocks corroborate the group
learned from the same confirmed recordings.

Only independently confirmed FPV files have established group membership.
Unassigned files can have competing date suggestions from different groups.
Rejecting an FPV/StickCam pair suppresses that pair in every group and counts
against other predictions for that FPV in the corresponding clock group. It
does not invalidate the entire recorder's clock or prove another group correct.

**Reject this clock alignment** rejects a particular predicted offset instead
of the underlying pair. Other group offsets and independent content evidence
remain reviewable. Rejections are source-bound and tied to the clock method and
start/end interpretation. They survive small changes to a model's median and
can be undone under **Rejected clock alignments**. Dates alone never confirm.

## Saved movement rhythm

The `gimbal-activity` cache holds observed tip-free gimbal activity and pose
quality at up to 10 Hz. The `radio` / `radio-review` cache holds derived stick
inputs, including observation gaps. Matching builds inexpensive rhythm features
from these raw caches. Their matching scores, offsets and agreeing three-second
sections are saved in results. No video decoding is needed to display an
existing fingerprint at the same source/calibration/settings revision.

**Movement rhythms & cross-modal evidence → Show saved rhythms** plots the same
prepared signal used by either experimental matcher, above/below the FPV
signal. Plots retain every 10 Hz matching sample and explicit gaps. They use
normalised activity units, not angular-position units. Green marks local
agreement at the loaded offset; the playback cursor follows Pair Review.
Use the time bounds to zoom and click a chart to seek the opened pair. Download
the plotted data as JSON when reporting a tracking or matching problem.

## Experimental motor audio and confirmed-pair study

**Analyse motor-sound rhythm** compares the selected FPV's embedded rotational
dynamics with StickCam audio, optionally searching every included StickCam.
It does not require FPV audio or stick calibration. Full-length audio is decoded
in bounded chunks and cached as `motor-audio`, with the configured frequency
band in the key (default 150–6000 Hz). Spectral flux and dominant-band frequency
changes provide a coarse activity cue; clipping, low tonal evidence and sudden
harmonic switches reduce usable observations. Audio/video stream start offsets
are included when provided by the container.

This is not an RPM or attitude estimate. A combined rotor sound cannot identify
individual motor speeds or signed roll/pitch/yaw. Throttle, distance, wind,
other drones, voices and microphone processing can alter the signal. NASA's
[measured small-UAV noise research](https://ntrs.nasa.gov/archive/nasa/casi.ntrs.nasa.gov/20160010139.pdf)
describes the changing acoustic signature caused by rotor-speed control and
gusts; this supports exploring an activity cue, not claiming a reliable
cross-modal matcher without reference evaluation.

**Extract signals from confirmed pairs** uses independently confirmed alignments
as reference material. Available inputs include generic foreground scene
activity after coarse affine camera compensation, saved gimbal activity and
stick-change rhythm, motor-band spectral changes, audio-band energy/flux,
FPV angular-rate changes, angular-speed magnitude, per-axis rate activity, and
FPV audio when present. Generic scene movement is explicitly not stick input.
Exact stick derivation is not started just to inspect a reference.

The study compares channels across modalities and combines available evidence
using repeated-section timing votes. Correlated channels within one modality
count as one source; another modality provides a fixed corroboration bonus. It shows a known reference offset separately from
an independently searched lag. Per-channel comparisons are exploratory and
have not been corrected for trying many combinations; their strongest score
must not be treated as a calibrated confidence. No per-pair weights, signs or
axis transformations are fitted to make the known pair agree. Results and
source-bound traces remain saved with the session and can be reopened.

These exploratory results do not modify confirmed pairings, automatically
confirm new matches, or teach camera-clock offsets. Preview shared visible
events and explicitly confirm before using a suggestion as an independent
anchor or exporting it.

**Search with combined rhythm** applies the same repeated-section consensus to a selected FPV
and one or all included StickCam recordings. It reuses full-length scene/audio
caches and any saved gimbal/stick data, uses embedded FPV motion by preference,
and can use already-saved visual FPV motion as fallback. It does not derive
precise stick tips. Results remain experimental candidates requiring an
independent visible-event check; no automatic pair or recorder assignment is
made. The motor-only search uses only StickCam motor-band audio and embedded
FPV motion. Both searches preserve competing candidates and require a margin
over other StickCam clips before labelling a suggestion.

## Independent overall movement

The `scene-activity` cache stores full-clip generic foreground movement at 10 Hz
separately from gimbal activity and detected stick positions. The **Overall
movement** attribute in the StickCam file list shows whether it is saved and
its observed coverage. **Motion comparison** has a separate overall-scene
movement row and toggle, alongside FPV rotation, detected sticks and both audio
waveforms. Units are image change, not degrees per second. Its shared cursor
and time window follow the other plots.

The dedicated rhythm review defaults to **Overall StickCam movement · repeated
sections**. Non-overlapping three-second FPV windows search the whole StickCam
activity trace and vote for a common offset. This rewards repeated content
evidence without assuming similar recording lengths. Competing peaks and
conservative eligibility gates remain visible. Source-current reference studies
record per-channel full-trace and repeated-section offsets separately and show
tracking coverage/recommendations.
