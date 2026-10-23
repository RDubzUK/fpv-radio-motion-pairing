# Tip-free gimbal movement matching

This experimental identifier compares **movement inside the radio's gimbals**
with **changes in FPV angular velocity**. It can shortlist flights when precise
stick tips are covered or unreliable. It does not reconstruct stick positions,
throttle, requested body rates, or exact frame synchronisation.

## Use it

1. Open **Calibrate & review StickCam** and find a clear, representative in-flight
   frame. Mark the radio outline and both gimbal regions/centres.
2. Select **Save radio regions · no tips needed**. Exact tips and rates are optional
   for this method. Suggested, unconfirmed tips are not promoted by this save.
3. In the guided workspace, select **Use audio + tip-free movement · skip exact
   inputs**, then **Run Quick Match**. Audio is attempted first when both feeds
   have audio. Unresolved clips use movement.
4. Alternatively, under **Find matching flights → Data to use**, select
   **Movement · no tip tracking** to compare movement without extracting audio.
   Both FPV embedded motion and FPV visual fallback are enabled by this preset.
5. Review **Tip-free gimbal movement suggestions**. Preview both sources at the
   proposed offset, check shared visible events and refine the timing. Confirm
   the independent-content checkbox to permit learning camera-date differences.

**Find StickCam counterpart** also supports the new method. Its automatic mode
uses tip-free movement when enabled in the guided workspace. Visual fallback
is enabled initially and can be switched off to avoid visual extraction.

Selecting **StickCam derived inputs** as well requests full stick tracking in
addition to gimbal activity. Turn it off or use the lighter preset when speed is
the priority. The existing derived-input rhythm remains a separate option.

## FPV recording types

- **Embedded gyro or attitude available:** reuse the shared motion cache and
  prefer the embedded source. DJI fused quaternions are differentiated into
  angular rates; they are not labelled raw gyro samples.
- **Vista, analog DVR, or other footage without supported telemetry:** derive
  experimental visual angular motion when that evidence source is selected.
  Preserve its unreliable intervals as gaps. A recording type or filename alone
  never establishes the presence or absence of telemetry.
- **No audio on one feed:** skip audio extraction and adaptive retries entirely
  when there is no possible audio pair. In a mixed batch, recordings without an
  audio stream are omitted from the audio retry queue.

Visual derivation can still be expensive or fail on blur, noise, low texture,
overlays, or ambiguous geometry. Movement matching cannot repair missing FPV
observations or a radio that is out of shot.

## Caching and progress

Gimbal activity uses reduced frames at up to **10 Hz**. It tracks the radio face
and movement in two small, stabilised gimbal crops, without tip template searches.
Compatible saved activity is reused in matching, counterpart searches and later
sessions using the same source/configuration.

The independent `gimbal-activity` cache key includes source identity/signature,
algorithm version, radio/gimbal geometry, appearance references and extraction
settings. Tip positions, stick Mode, rate profiles, signs and diagnostic expected
control values do not invalidate or fabricate activity samples. Changing the
radio geometry or its source does invalidate the relevant cache.

The file list shows **Gimbal movement**, whether it is saved, its observed
coverage, or whether radio regions are needed. Per-video extraction percent,
elapsed time and ETA appear in the normal matching progress area. Extraction
duration is saved with each fingerprint. Portable project bundles include this
cache when requested.

Up to six validated radio appearances are used: the initial calibration and
available full-frame tracking landmarks. A different view is reacquired against
its actual image; neither expected controls nor a covered tip can establish a
lost radio pose. Switching appearance or recovering from loss creates a gap in
the motion derivative rather than an artificial movement burst.

## Evidence and review policy

For audio-free O4 footage, [O4 Quick Match](O4_QUICK_MATCH.md) can find radio
regions automatically and prepare StickCam clips sequentially until a supported
seed is found. It uses that seed for clearly provisional modified-date suggestions
and optional independent movement spot checks. Confirmed camera clocks remain
separate and require independently reviewed content.

Rows without positive content evidence or an explicit saved/date alignment have
no estimated offset. They cannot be opened as automatic candidates. A missing
calibration is shown as **Radio regions needed**, rather than selecting the first
FPV recording at a placeholder zero offset. A genuine measured zero offset is
still valid. Generic review controls, counterpart results and flight previews use
the selected gimbal/rhythm method's own offset, score and overlapping interval.

If neither feed can form an audio comparison and no selected StickCam movement
method has a usable calibration, ordinary matching stops with a setup message.
The embedded-motion seed route instead attempts automatic region preparation. Save the
radio regions for tip-free activity; derived-input rhythm also needs full stick
input calibration and derivation.

- Search all feasible offsets using content only. Dates and length similarity
  do not narrow or boost the movement score.
- Require at least **8 seconds of observed common evidence**, even when the
  general minimum overlap is shorter. Short recordings can still be reviewed
  manually or matched by other evidence.
- Count separate, active three-second sections at one common offset; preserve
  missing observations and require temporal spread.
- Report the heuristic score, competing StickCam separation, timing peak margin,
  observed seconds and local correlations. These are not probabilities.
- Permit several FPV parts to belong to a StickCam flight. A second FPV segment
  is not treated as a competing StickCam owner.
- Keep movement proposals out of automatic assignment and camera-clock matrices.
  Only an independently reviewed, source-current confirmation can teach dates.
- The coarse timing resolution is **0.1 seconds plus unknown camera/controller
  response delay**. Refine against visible events before exports needing exact
  sync.

The initial four-pair study is documented in
[`artifacts/pair-validation/gridworks-oct26/README.md`](../artifacts/pair-validation/gridworks-oct26/README.md).
It motivates this experimental option; it does not establish a general false
match rate. No production threshold is presented as a calibrated confidence.

## Five-pair benchmark

The [13 October benchmark](../artifacts/pair-validation/movement-blind-20261013/README.md)
searched all 25 combinations of five known pairs using only anonymous movement
traces. Four correct pairs passed the review gates, with no incorrect suggestions;
their offsets were within 0.11 seconds of the saved references. The fifth remained
unresolved because its radio tracking produced too little usable evidence.

On this RTX 3090 machine, fresh activity extraction took 8m31s for 20m31s of
StickCam footage, around 25 seconds per minute. Five FPV telemetry extractions
took 53 seconds; all comparisons took 0.052 seconds. The raw caches loaded again
in 0.24 seconds total. These figures exclude manual calibration.

Four clips had helped develop the method, so this is a blinded regression run
with one new case. All five FPV clips used embedded attitude; visual-only matching
and a fresh independent validation set remain untested.
