# FPV ↔ StickCam Matcher

A local Python engine and LAN web workspace for pairing FPV recordings with videos of the pilot's radio. Choose sources, try cached boundary audio first, resolve calibration/matching exceptions, review synchronized pairs, and export their shared intervals for editing. The guided workspace keeps technical tools available on demand.

Earlier versions were reviewed with synthetic signals, generated videos and browser workflows. The latest tracker, staged matching gates and optional stabilized export have received static review and browser rendering inspection; their accuracy and output still need real-footage evaluation. Correlation scores and the “strong” label are evidence summaries, not calibrated probabilities.

## Open the workspace

On this machine the managed service is available at:

- **LAN:** <http://192.168.0.114:8767>
- **On the server:** <http://127.0.0.1:8767>

The service binds to `0.0.0.0:8767`, runs independently of this coding session, and starts at user login. The LAN address can change with the network configuration.

### GPU tracking and live inputs

**Tracking processor** under **Saved StickCam movement data** defaults to **GPU when available**. CUDA-enabled OpenCV accelerates forward/reverse optical flow, perspective crops and stick template matching. CPU SIFT reacquisition, geometry, appearance checks and confidence checks remain active. Routine radio reacquisition searches a generous region around the last accepted outline; periodic full-frame searches recover when the radio moves outside it, and large/invalid regions fall back to full-frame immediately. Progress and per-video derivation logs show time spent decoding, tracking the radio and gimbals, CUDA operations/uploads, and SIFT searches. The same appearance, geometry and confidence checks still gate accepted poses. **CPU** explicitly disables GPU tracking; **CUDA only** reports an error if it cannot run. Decoder selection is independent. Current valid StickCam caches are reused with either processor. Stage timings are diagnostic wall time, not a speed benchmark; some substage times are included within radio/gimbal totals.

The live **Inputs as they are derived** chart shows the latest 20 seconds of provisional throttle, yaw, pitch and roll observations, with channel values and quality. It follows the selected stick Mode and preserves unobserved samples as gaps. It is available in newly started derivations; an existing worker cannot add live output midway through a run. The full validated recording is saved when extraction finishes.

The standard PyPI OpenCV wheel is CPU-only. On this machine an official OpenCV 4.14.0 CUDA wheel has been built and installed in `.venv` for the RTX 3090. To reproduce it with an installed NVIDIA driver and CUDA toolkit, run `tools/build_opencv_cuda.sh` **after** normal environment setup. It builds official pinned sources under ignored `.gpu-build`, detects the first GPU's architecture and installs only into this project's environment. `FPV_CUDA_ARCH` and `FPV_BUILD_JOBS` override architecture and build concurrency. A normal `uv sync` may restore the CPU wheel; rerun the CUDA installer afterwards. The custom wheel depends on the installed CUDA runtime and is not a portable binary.

### Improved visual motion and optional gap filling

FPV visual preparation now distributes landmarks across the image, retains tracked landmarks independently of the diagnostic 2D affine fit and can reacquire large movements using mutual descriptor matches. **Track intermediate frames for fast motion** tracks at source frame rate, up to 120 Hz, then composes observed rotation increments into the selected output intervals. This uses more processing than tracking only at the output Hz; disable the option to favor speed. Embedded gyro remains preferred. Lens FOV/projection, stabilization, parallax, blur and featureless sky still limit image-derived rotation.

The viewer reports reasons for missing rotation samples. **Fill short gaps** is off by default. When enabled, it linearly interpolates only between two observed samples whose total separation is within the selected 0.10–0.50 second span. Dashed segments identify those display estimates; they do not change the cache, observed coverage or matching evidence. Long gaps and leading/trailing gaps remain unfilled. Older algorithm caches are retained for review but must be re-derived to use the improved extraction. Accurate lens geometry and a comparison against known gyro are still needed to establish accuracy; no performance or accuracy gain has been measured for this version.

Select a saved session in **Sessions** to rename it; the new name is saved with that session.

```bash
systemctl --user status fpv-radio-motion-pairing
systemctl --user restart fpv-radio-motion-pairing
journalctl --user -u fpv-radio-motion-pairing -f
```

For a manual launch:

```bash
./start.sh
# Or use the installed command:
.venv/bin/fpv-match-web --host 0.0.0.0 --port 8767
```

Use `--host 127.0.0.1` when you want access only from the server itself. Stop the managed service before starting another process on the same port.

## Install on another machine

Requires Python 3.11+ and FFmpeg/ffprobe, including the `libx264` and `dnxhd` encoders.

```bash
uv sync --frozen
./start.sh
```

The Python version and dependencies are recorded in `.python-version`, `pyproject.toml` and `uv.lock`. Without uv:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
```

The user service example is in `deploy/fpv-radio-motion-pairing.service`. Adjust its project path when installing elsewhere.

## Recommended workflow

1. Name the session and choose FPV/StickCam sources. Use **Choose all, some or one** to limit the target set.
2. Choose **Automatic · audio first** and **Find matching flights**. It reads cached audio first; distinct content proposals skip motion. Unresolved clips reuse current StickCam inputs and embedded FPV motion where calibration is available. Experimental FPV visual fallback is off by default.
3. Use **Exceptions & next actions** to restore/relink sources, calibrate uncertain clips or inspect tracking coverage. Calibration is unnecessary for audio-only matching.
4. **Review next pair** plays both feeds over the shared interval without motion extraction. Optional audio waveforms are independent of calibration. Check events and timing, then save the alignment.
5. Export confirmed aligned clips, or enter the 9:16 editor. Several FPV segments can share one StickCam; timestamp predictions still require review.

**Technical tools** exposes calibration, FPV preparation, evidence selection, rates, source/IMU mapping, extraction ratios, charts and logs. Last successful results remain viewable during a rerun or after cancellation/failure. Current decisions are bound to source signatures; changed files and legacy confirmations without signatures require review again.

See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the complete process and outstanding validation, and [WORKFLOW_REVIEW.md](WORKFLOW_REVIEW.md) for the corrections in this review.

## Detailed controls

### 1. Add recordings

Choose **Use folders on the server** for media already on the machine. Use the separate **Browse…** button beside each source path to navigate the server's folders and inspect supported video files before choosing. SMB/CIFS shares mounted on the server appear as shortcuts in the folder picker; mount a share on the server first, then browse it there. Files are read in place; the app does not duplicate them. Each input can be a directory or a single video. Directories are scanned at their top level.

Alternatively, choose files from your browser. Uploads go directly to the saved session in 8 MiB chunks. A paused or interrupted upload resumes from its last completed chunk after reselecting the original unfinished files. Each file can be up to 128 GiB; there is no fixed video-count cap. Larger sessions take more processing time; selecting a subset for matching reduces comparisons. Filenames must be unique within a group.

Supported extensions: MP4, MOV, MKV, AVI, M4V and WebM. Decoding depends on FFmpeg's available codecs.

### Calibrate unresolved StickCam

**Saved StickCam movement data → StickCam tracking** offers fixed extraction Hz (the default), **0.5 × source fps**, or **0.25 × source fps**. Each recording's average frame rate is detected independently: a 29.97 fps clip uses about 14.985 Hz or 7.493 Hz. The selected clip and preparation table show source fps and the requested tracking Hz. Ratio tracking supports up to 60 Hz; select a smaller ratio or fixed Hz for faster source recordings.

The selection applies to preparing StickCam data, matching and motion comparison, and is saved with the session when preparation or matching starts. Cache keys include the effective per-clip tracking rate and calibration; changing the ratio requires derivation only if that rate's cache is missing. Saved data at other rates remains available when switching back. FPV extraction and comparison Hz remain separate. Reduced tracking uses fewer frames in the computer vision work, while video decoding still costs time. Fast stick movements may be missed; interpolation to a higher comparison Hz cannot recover them. Variable frame rate clips are sampled on a regular time grid based on their detected average fps.

The command line equivalent is `--stick-sampling-ratio 0.5` or `--stick-sampling-ratio 0.25`; omit it to use `--sample-fps` for StickCam.

#### Upside-down StickCam and the session radio

In **Frame & calibration → Session radio & orientation**, select **Upright (0°)** or **Upside down (180°)** for the calibration frame. Regions refer to the radio's physical controls when viewed upright; the physical left gimbal appears on the image's right when inverted. Changing orientation swaps existing region labels when needed. Review both regions and tips before confirming the calibration. Inverted extraction reverses both stick directions in the reference frame; later camera motion remains handled by relative radio tracking. The centred radio preview also uses the confirmed upright basis. Calibration images remain in their recorded orientation so markers keep source coordinates.

Once you have a clear, calibrated radio with its outline, enter its model or a custom name and click **Confirm radio & use this frame as reference**. This saves the expected shape and appearance for the session. Subsequent clips are checked automatically when opened, or with **Check midpoint** / **Check all clip orientations**. Checks try the midpoint, then nearby ±5/±10 second frames if needed, requiring distributed feature matches on the saved radio face. The suggested identity is the confirmed reference's label, rather than a manufacturer guess from an unverified silhouette. You can correct the label and replace the reference.

The review shows the sampled frame with the matched outline and physical gimbal labels, plus a separate result for the current calibration frame. **Use suggested calibration orientation** applies a draft; **Confirm calibration** saves it. A different orientation at the midpoint is flagged rather than applying a clip-wide flip. Obscured radios, failed appearance matches and near-sideways views remain unknown and can be set manually. Results persist with the session and are keyed to the reference revision, source signature and calibration time; image-only checks do not derive the whole clip. Upright movement caches remain reusable; an inverted calibration uses a distinct movement cache.

For each recording:

1. Choose a frame where both gimbals and stick tips are visible. Thumbed and pinched grips both work: click the centre of the visible stick tip, not the thumb or finger.
2. Choose **Wonky StickCam** (default) or **Fixed StickCam** from the **StickCam mode** dropdown. Saved clips retain their configured mode when reopened.
3. Choose **Suggest gimbal regions**. With no marked outline, it first checks up to three saved motion calibrations in this session that include a radio outline. Distributed, mutual feature matches and a validated perspective transform can transfer that radio's outline, housing boxes and neutral centres to the new frame. It identifies a saved appearance, not a manufacturer/model. It also searches the new frame for current stick tips using confirmed tip appearances or distinct visible caps. Dashed circles mark suggested tips and crosses mark neutral centres. Inspect every marker and use the tip or centre tools to correct it, then save or check tracking. Ambiguous/covered tips are left unmarked and the message names the tips that need manual selection. Without a reliable reference match it searches for two housings with visible rim edges; pair geometry alone is insufficient. If necessary, click **Track radio outline** and mark top left, top right, bottom right, bottom left to constrain that search to the radio face. Blur, occlusion, different cases and viewing angles can still require a clearer frame or manual regions. Suggestions do not save a calibration automatically.
4. Click the centre of each visible stick tip. Suggestions do not select tips. Neutral centres default to each box's midpoint and can be marked separately.
5. The outline also supplies a tracked affine transform to estimate camera-relative radio orientation and compensate stick coordinates. The stick regions are excluded from body tracking features.
6. Save the calibration. **Save & next clip** advances to the next unconfigured recording.
7. Use **Check 3 seconds of tracking** to inspect the moving markers and radio orientation trace.

Calibration stills are decoded on the server, so browser support for the original video codec is unnecessary for marking regions. Regions and coordinates are source-image pixels, including when the displayed image is smaller.

**Copy regions to other clips** scales the geometry to matching aspect ratios. Each copied clip still needs its own tip selections. Wonky tracking starts at the chosen reference frame; earlier frames have no motion evidence. The estimated offset remains relative to the full original recordings.

Choose **Audio only** when reliable tip tracking is impossible. Such pairs need review and can remain unmatched if the microphones share insufficient sound. **Exclude this clip** leaves an unusable recording out of the analysis without deleting the original.

Mode 2 is assumed: left stick = yaw/throttle; right stick = roll/pitch. Full occlusion, severe perspective changes and transmitter movement out of frame can defeat the tracker. Tracking quality alone cannot establish that the correct object was selected; inspect the markers.

### Matching a single target

Choose all recordings, one StickCam against the FPV set, or one FPV video against the StickCam set. A single-target run compares only the selected clip with the opposite-side candidates. Enable **Allow unconfigured StickCam with audio + filename clocks** to include uncalibrated StickCam candidates without motion evidence. If distinctive boundary audio establishes a strong single-target anchor, the matcher can infer a tentative camera-clock offset and use filename times to help rank overlaps. Dates alone cannot establish a match when the cameras' clock relationship is unknown, so review the proposals.

### Optional flight-controller rates

If you enable a rates profile under analysis settings, the app maps normalized roll, pitch and yaw stick positions through Betaflight **ACTUAL**, legacy **Betaflight**, or **KISS** curves before comparison. Enter each axis from the flight-controller rate profile; KISS values use the GUI's decimal scales (for example, 0.7). Throttle is unchanged. The curves can account for expo and response shape. Correlation already normalizes each axis, so a uniform increase in maximum degrees per second is mostly removed; rates are optional evidence shaping, not a guarantee of better matches. Profiles apply to every FPV recording in that analysis. The formulas follow Betaflight's [rates overview](https://betaflight.com/docs/wiki/guides/current/Rate-Calculator) and [firmware implementation](https://github.com/betaflight/betaflight/blob/master/src/main/fc/rc.c).

### 3. Analyze

Defaults are 30 Hz extraction, 30 Hz candidate comparison, two extraction workers, two seconds minimum usable overlap and a 0.55 proposal threshold. Automatic mode screens audio first, requiring repeated distinctive evidence, a consistent offset and separation from rivals. Motion proposals additionally need usable observations, multiple axes and a distinct timing peak.

Motion, audio and metadata are extracted once per recording and cached. Changing comparison settings can reuse those fingerprints. Cancellation stops the analysis process and its decoders; rerunning reuses completed cache entries. Bad files are reported individually when possible.

Completed sessions, calibrations, corrections and exports survive server restarts. Interrupted work is marked explicitly and can be rerun. Media and results are stored under `~/.fpv-radio-motion-pairing`; set `FPV_MATCHER_DATA` before starting the server to use another storage location.

### 4. Review and export

Open a proposed pair, or review an unmatched clip's best candidate. The FPV selector lets you try alternatives. Both videos share a timeline covering only their overlapping interval.

- Positive offset means FPV starts later: trim that amount from StickCam.
- Negative offset means StickCam starts later: trim the absolute amount from FPV.
- The frame buttons move the shared cursor or adjust the offset at the selected export rate.
- Choose one microphone for playback, or mute both.
- **Fast preview · 480p on demand** starts from the requested source time, prepares a four-second H.264/AAC fragment and buffers a small amount ahead. It does not queue an entire-video proxy. Seeking requests fragments at the new position; both videos keep source-relative timestamps for paired playback. Uncheck it to try supported original files. Unsupported originals fall back to fast preview. Analysis and exports use the originals.
- Save each FPV segment’s pairing and alignment, or reject that segment. One StickCam can hold several FPV segments; saving another does not replace earlier segments.

Export a single pair or all reviewed pairs. Available profiles:

| Profile | Output | Intended use |
| --- | --- | --- |
| Compact | H.264 CRF 18, 8-bit MP4; AAC audio | Smaller files and general playback |
| Resolve editing | DNxHR HQX, 10-bit MOV; 24-bit PCM audio | Edit codec and Linux Resolve workflows; substantially larger files |

Blackmagic's [Resolve 20 codec guide](https://documents.blackmagicdesign.com/SupportNotes/DaVinci_Resolve_20_Supported_Codec_List.pdf) lists DNxHR and PCM support on Linux, while H.264 decoding is Studio-only and AAC is unsupported there. The editing profile provides an alternative for those installations.

Exports keep each source's pixel dimensions, rounded down to even dimensions if necessary. Both clips have the same constant frame rate, frame count and duration, and begin at timeline zero. Choose 24, 25, 30, 50 or 60 fps to suit your edit. Source colour tags are carried through when available; the compact profile is 8-bit and does not preserve a 10-bit source's bit depth.

Each pair includes `alignment.json` with original trim points, source metadata, the chosen offset, output filenames, rate and frame count. Put the two exported clips at the same timeline position in Resolve. Correct alignment still depends on the estimate or saved manual correction; constant frame rate export does not make an uncertain estimate frame accurate.

Individual files can be downloaded separately. ZIP downloads stream from the generated files with bounded buffering, so a large batch needs one stored copy of its exported clips. Repeating an identical export request reuses the existing task/result.

## Matching evidence

### Motion

FPV motion now prefers embedded camera telemetry. `telemetry-parser` decodes supported vendor formats. Native gyro supplies sensor angular rates; DJI fused quaternions supply derived local angular rates using rotation increments without Euler wrapping. These are mapped to drone roll/pitch/yaw using the configured signed sensor mapping and camera uptilt. Matching compares visible stick positions, optionally transformed by your rates model, with measured body rates. It does not treat StickCam camera gyro as stick input.

In **Find matching flights → Data to use for matching**, choose embedded FPV gyro/attitude, FPV derived visual motion, StickCam derived inputs, FPV audio, StickCam audio and filename timestamps independently. **All available**, **Audio + timestamps** and **Gyro + sticks** presets provide starting selections. Choices are saved with the session when analysis starts and recorded in results. Disabled sources skip extraction, cache loading and scoring; existing caches are retained for later runs.

Motion matching needs StickCam inputs and at least one FPV motion source. With both FPV motion sources selected, embedded data takes priority and visual extraction runs only if it is unavailable. Audio matching needs both audio sources; selecting only one skips audio extraction on both sides. The audio preset skips all motion extraction and StickCam calibration requirements. Filename clocks need an independent audio or motion anchor and cannot establish a match alone. With embedded-only motion selected, unsupported FPV recordings can still participate through selected audio; without audio they report an error. Rates remap saved StickCam inputs only when motion matching is enabled. These matching choices do not restrict the separate Motion comparison or manual StickCam preparation tools.

Open **FPV gyro and camera orientation** before motion analysis to set camera uptilt, sensor mapping and gyro-to-video shift. The initial mapping `zXy` assumes right/up/backwards camera axes, and zero uptilt is an assumption that should be corrected. Mount settings apply across the session and are recorded in results. Existing matches must be analysed again to use the new engine. Native gyro and fused attitude-derived rates are labelled separately.

Visual fallback uses sparse Lucas–Kanade flow, forward/backward rejection and a RANSAC affine estimate. Image-centre translation is normalised by frame dimensions and converted to rates. These are approximate 2D proxies, not calibrated drone angular rates. Fast inverted freestyle, parallax and stabilisation can invalidate their axis mapping. Visual tracking currently samples at the extraction frequency before estimating motion; later refinement cannot recover discarded observations. A calibrated visual 3D fallback and gravity/throttle dynamics remain planned in [FREESTYLE_MOTION_DESIGN.md](FREESTYLE_MOTION_DESIGN.md). Neither gyro nor an orientation stream alone proves world-gravity/thrust behaviour; throttle is not scored against translation.

**Motion comparison** works before full analysis: choose an FPV and optionally a configured StickCam and load signals. FPV rates or available quaternion orientation appear above detected stick/requested-rate traces and throttle. Zoom, pan, toggle axes and inspect shared cursor values; window requests preserve extrema and tracking gaps with bounded payloads. Normalised amplitude is a display aid, not a physical axis calibration. Chart shifts are exploratory; save real alignment in the shared video preview.

**Centred StickCam preview** uses a saved four-corner radio outline to centre and level the visible shell, fitting its corners with a margin and smoothly changing zoom. Open the source playback using the button in that section. Usable StickCam gyro supports only short outline gaps after agreeing with trusted outline rotations; otherwise the preview uses outline observations. Missing outline tracking shows the full frame. It cannot reconstruct radio parts outside the source or correct arbitrary 3D perspective; optional centered/level separate StickCam exports now use current saved outline observations. Choose **Prepare this StickCam for stabilization**, then the **Centered & level** output option. The radio stays inside the crop and all crop corners must map to recorded pixels; lost/unsafe intervals retain original framing. The alignment manifest reports usable and fallback intervals. This native FFmpeg path has not yet been encoded/evaluated on real footage.

Selected tip appearance is checked for distinct matches within calibrated travel. Confirmed radio landmarks anchor a perspective transform and rectify both gimbal crops. Distributed shell checks reject drift and out-of-frame outlines; lost shell or occluded/ambiguous tips produce quality gaps. Generic dark housing blobs no longer replace missing confirmed tips. Cache status reports per-axis and jointly observed coverage, separately from storage readiness.

Fingerprints are resampled onto a common clip-relative grid, with smoothing before reducing the rate. Gaps remain invalid. Weighted correlations compare the corresponding body-rate axes for telemetry, or approximate roll/rotation, yaw/horizontal-flow and pitch/vertical-flow axes for visual fallback. The visual path tests position or velocity; the telemetry path uses position/rate commands. Both retain stick/image polarity handling, and combine continuous signals with activity bursts. Short accidental overlaps are downweighted. Likely motion alignments are refined at the extraction rate.

Global assignment includes unmatched options. Scores below the proposal threshold cannot force a pairing. Candidate separation and competing timing peaks determine the review label; audio-assisted and clock-assisted proposals remain review candidates.

### Audio

Only the first and last 30 seconds are decoded. For clips shorter than a minute those boundaries cover the whole clip.

The fingerprint includes energy, spectral flux, frequency-band trends and frequency-peak pair hashes. Repetitive hashes are suppressed to reduce constant motor-tone evidence. Matching hashes vote for a common offset; competing peaks and independent matching hashes determine whether that evidence is usable. Audio can reinforce motion, refine a supported start point, or propose a distinctly shared boundary segment when stick tracking is weak.

The two microphones may hear different sounds. Missing, constant or unrelated audio is optional evidence and does not prevent motion matching.

### Filename clocks

Common compact and separated date/time patterns are recognised. A 1970 date can still provide a useful relative clock reading; it is not treated as a trustworthy wall-clock date. Repeated/reset readings cannot establish a stable ordering.

For a matching pair:

```text
FPV filename time − StickCam filename time − video start offset
    = inter-camera clock difference
```

Distinctive independent pairs establish a consensus. Once supported, this difference helps search plausible alignment windows for other recordings, with overlap checks and a review label. Missing or inconsistent timestamps leave motion/audio matching available. Use separate sessions when the camera-clock relationship changes.

## Batch cost

The intended workload is 10–40 pairs at 1080p–4K.

- FFmpeg sends sampled, reduced grayscale frames to Python: longest side 960 pixels for FPV, 1280 for StickCam.
- Decoder, filter, OpenCV and encoder threads are bounded.
- One analysis job and one export job run at a time; extraction within an analysis has 1–4 configurable workers. Fast preview permits two concurrent chunk encodes for a video pair. Preview fragments use a separate cache with a 1 GiB eviction target, retaining recently accessed fragments; browser buffers retain only nearby footage. CUDA decode/resize is attempted on supported NVIDIA systems with CPU fallback, while the H.264 preview encoder uses the ultrafast CPU preset. Existing whole-video proxy API requests are supported for older clients, but the current frontend does not create them.
- Audio fingerprints cover at most a minute, independent of recording length.
- Candidate comparison is coarse; only likely alignments use the finer rate.
- Uploaded files use content hashes for cache identity; local media uses its path, size and modification time.
- Upload capacity is checked before transfer and during chunks. DNxHR exports have a conservative storage preflight.

A synthetic benchmark of 40 × 40 five-minute fingerprints took about **16 seconds** with **334 MiB peak RSS** on this machine. All 40 intended pairs were selected. This measures comparison work only. Decoding 80 real 4K clips, extracting motion, creating proxies and exporting media adds time and depends on codec, duration and hardware.

## CLI and engine

```bash
.venv/bin/fpv-match --fpv /path/to/fpv --radio /path/to/stickcam \
  --config /path/to/calibration.json --output ./results \
  --sample-fps 30 --match-fps 30 --workers 2
```

The config can be a single calibration (see `radio_rois.example.json`) or per-recording entries:

```json
{
  "clips": {
    "stickcam_01.mp4": {"exclude": true},
    "stickcam_02.mp4": {"skip_motion": true},
    "stickcam_03.mp4": {
      "stickcam_mode": "fixed",
      "reference_time": 0,
      "left_gimbal": {"x": 100, "y": 300, "width": 250, "height": 250, "tip_x": 125, "tip_y": 125},
      "right_gimbal": {"x": 400, "y": 300, "width": 250, "height": 250, "tip_x": 125, "tip_y": 125}
    }
  }
}
```

Those example coordinates must be calibrated to your source. The web workflow requires explicit tips for calibrated movement extraction. Reference validity and observation gaps must be inspected before relying on estimated control inputs.

Outputs include `matches.csv`, `results.json`, `progress.json` and automatic alignment plots. The web CSV includes saved manual corrections. `run_batch` in `fpv_matcher/pipeline.py` is the reusable engine; it has no web or Resolve dependency. `fpv_matcher/motion/base.py` defines the motion estimator interface.

See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the consolidated scope and [REVIEW.md](REVIEW.md) for the review evidence and remaining field validation.


## Saved rate presets and extraction progress

`rate_presets.yaml` in the project stores named ACTUAL, Betaflight and KISS profiles. Use **Save rates to YAML** in the rates section, optionally setting the preset as the default for new sessions. Load a saved preset into an existing session using **Load preset**. Editing YAML is supported; it is read again on load and session creation. Existing sessions retain their saved analysis settings. The CLI uses the YAML default, or `--rate-preset NAME`; `--no-rates` disables it. The initial file has no assumed pilot rates.

Stick and visual-motion extraction now report percentage complete and an approximate per-clip ETA after at least three seconds of observed processing. Batch progress includes active clip bars; Motion comparison has a stage bar and ETA. The estimate covers the current motion extraction, not subsequent audio/assignment/export. It includes time spent waiting for decode/I/O, so changes in SMB speed, codec or tracking workload can change it. Cached clips can complete almost immediately. A first observed 200.6-second 4K StickCam with outline tracking took about 188 seconds to detect and package its signals on this machine; this is an observation, not a guarantee for other clips.


## Portrait pair editor

After checking synchronization, choose **Save alignment & open 9:16 editor** in the shared preview. The canvas exports at 1080 × 1920, with FPV in the top 60% and StickCam in the bottom 40% initially. Each pane has independent zoom, horizontal/vertical position and rotation. Content is clipped at the horizontal split, even when rotated. Adjust the split or the shared in/out points for a highlight.

The main FPV image defaults to 60% of the scale needed to fill its pane, with a blurred copy filling the surrounding space to retain more width. Choose black background or increase zoom if preferred. StickCam defaults to 100% fill. The preview and export use the same geometry; browser playback/proxies are for review and export reads the originals. Select audio from either source or mute it.

Layouts persist per video pair. Export creates one H.264/AAC portrait MP4 and an alignment/layout JSON, through the cancellable export queue with normal download links. The shared recording offset is applied before compositing. The editor uses static framing controls; the separate outline/gyro stabilisation preview is not baked into the composite. The exporter checks dimensions, duration and frame count after encoding; actual composite export has not been run during this implementation.

## Decoding and tracking speed

Automatic decoding tries NVIDIA CUDA for supported H.264/HEVC clips, resizes on the GPU and transfers reduced grayscale frames to the CPU tracker. If CUDA cannot initialise, it falls back to CPU before emitting frames. Rotated streams use CPU in automatic mode. Select CPU or CUDA explicitly in the motion settings, or use `--decode-mode` in the CLI. This machine has an RTX 3090; its OpenCV build still performs optical flow on the CPU. GPU decoding is not a guarantee of faster extraction, particularly with slow network storage.

For Wonky StickCam, a reliable radio outline transform is now shared by both gimbal trackers. Separate housing tracking resumes when outline quality drops. This avoids repeating full-frame optical-flow work while the outline is reliable. The changed tracking algorithm invalidates older stick extraction caches.

Automatic FPV motion always prefers usable embedded gyro samples, then angular rates derived from embedded orientation. Image-derived motion is an experimental fallback when neither can be read and the user has enabled visual matching. The selected source is recorded in results and shown in Motion comparison. Camera axis mapping, mount uptilt and gyro timing remain explicit calibration settings.

After loading calibrated StickCam signals, expand **Find FPV clips by gyro correlation** and choose **Scan all FPV gyro**. The scan reuses that StickCam extraction and compares it against every session FPV clip with supported embedded gyro or orientation. Telemetry and unsupported-source results are cached by file identity and parser version, shared with batch matching and charts. Changed source files invalidate them.

The table ranks candidates with motion scores, roll/pitch/yaw strengths, proposed recording offsets, valid overlap and separation from other timing peaks/clips. A distinct candidate needs multiple axes and adequate support; the scan can return no clear match. **Plot at offset** opens the proposed alignment in the existing charts. Scan results persist with the session, support cancellation and do not automatically pair clips. This scan omits visual fallback, audio and filename evidence so that the displayed score describes embedded-motion correlation.

Recording lengths are not pairing evidence. Motion correlation searches relative offsets and scores the common signal interval, so a short flight clip can match a section inside a much longer recording. Durations only bound valid overlap and preview/export trims; the minimum shared evidence is specified in seconds. Clock-assisted refinement also uses measured shared evidence, without requiring overlap to cover a fraction of either recording. Audio currently samples the first/last 30 seconds of each source, so shared sound located only in an unsampled middle section will not contribute.

**Open synced videos**, below the motion charts, loads FPV and StickCam together even before batch analysis. Choose side-by-side or stacked layout, audio source, shared play/pause, seek or 30 Hz frame steps. The FPV shift above controls their shared interval. Clicking a chart seeks this preview and playback moves the chart cursor. The browser corrects drift and waits for both videos when buffering; playback is a review aid, while exported clips use exact frame counts.

Motion confidence now includes non-overlapping 3-second sections at the proposed offset. Each usable section needs at least two active axes with sufficient valid tracking; a supported section needs signed correlation ≥0.60 on two axes with polarity fixed across the overlap. Quiet sections and poor tracking are excluded, so zero usable sections means insufficient evidence. Counts and agreement percentage appear in charts, gyro scan candidates and newly analysed pair cards/results/CSV. Strong motion proposals additionally require at least five supported sections, ≥60% agreement and support in at least two thirds of the overlapping timeline, plus the existing score, tracking and competing-peak/candidate checks. These are heuristic evidence thresholds, not calibrated match probabilities; audio-only proposals remain marked for review.

The included current telemetry-parser source replaces the older published wheel for newer DJI formats. See [vendor/TELEMETRY_SOURCE.md](vendor/TELEMETRY_SOURCE.md) for its pinned upstream revision, local build metadata adjustments and initial Rust build requirements. Use `uv sync` to install the included source; a plain package install from PyPI would use the older published parser. Parser-version changes invalidate positive and unsupported telemetry cache entries.

### Prepare and reuse all StickCam movement data

After calibrating radio outlines/gimbals/tips, choose **Derive all calibrated StickCam videos** in **Saved StickCam movement data**. This prepares every calibrated, non-excluded recording, skips audio-only recordings and reuses existing valid extraction. A missing calibration remains visibly pending. The per-video table shows cache status, sample count, extraction rate and save time; progress and the derivation log report reused, newly derived and failed clips independently. Completed data survives cancellation and server restarts. Preparation can be cancelled; matching waits for preparation to finish so the same extraction is not run twice concurrently.

Raw roll/pitch/yaw/throttle inputs, tracking quality and tracked outline coordinates are persisted as compressed NumPy fingerprints in `~/.fpv-radio-motion-pairing/cache`. Matching, chart review and preparation share one extraction/cache contract. Source identity, calibration, sample frequency and tracker version control reuse. Changing FPV rate curves or matching thresholds reuses the stored raw inputs; changing source footage or calibration requires fresh extraction. A cache status means data is saved, not that tip tracking or the resulting match has been confirmed correct.

A calibration frame can be anywhere in a clip. Derivation tracks forwards and backwards from that frame, then sorts all samples into the original recording timeline. Backwards decoding uses chunks of at most 60 reduced grayscale frames rather than loading the full clip into memory. Older extractions that started at a later calibration frame require a fresh full-recording extraction; existing full extractions calibrated at zero remain reusable. The cache table includes the saved sample time range.

### Waveforms and visible section evidence

Enable **Show audio waveforms** in Motion comparison to load both recordings' audio envelopes. The full waveforms are decoded once with bounded streaming memory, cached on disk and reduced to peak-preserving viewport data. They share the motion charts' timing offset, cursor, zoom/pan and click-to-seek controls. Clips without an audio track show an explicit empty state.

**Highlight matching sections in green** shades the same shared time ranges on both motion plots and, independently, both audio waveforms. Motion bands use the existing 3-second, two-active-axis agreement checks. Audio bands assess only jointly sampled first/last 30-second boundaries at the displayed offset: a section needs local spectral fingerprint support (at least 12 distinct shared hashes spanning 1.5 seconds within 150 ms of the shift), or correlation ≥0.65 on at least three active audio trends. Final partial sections shorter than two seconds are omitted. Quiet/insufficient evidence is excluded; unsampled middle audio is unassessed. These heuristic bands help inspect an alignment and never save/confirm a pair automatically.

Fast preview uses the browser's Media Source API with baseline H.264, optional AAC audio, preserved aspect ratio and a maximum 480p/30 Hz review image. Each independently encoded fragment reports its first video timestamp so AAC encoder priming does not change the source clock between chunks. Buffering pauses both paired feeds until they can resume together. Large seeks abort stale requests; leaving the preview cancels pending work. A browser without compatible Media Source support must use a supported original or a current Chrome/Edge browser. With fast preview unchecked, playback tries originals and requires an actual decoded video frame before accepting codec support. Original recordings remain the source for motion extraction and exports. Lower preview resolution and 30 Hz playback are review aids, not substitutes for checking source frames in final editing.

### Calibration review and clip details

**Find radio in this clip** and **Auto-calibrate remaining** search for usable frames at five-second intervals (default first 60 seconds). Confirmed outlines provide appearance references; missing or cropped radio views are skipped. Suggestions remain editable drafts until **Confirm calibration & next clip** saves them. Previous/next clip and ±5-second controls help review frames; tracking checks can inspect a draft without saving it.

Clip lists and selectors show source durations when metadata is available. Metadata loads with two bounded requests at a time and is cached by source file signature. Duration is display information, not evidence for pairing. The rates tables display each axis’s full-stick maximum rotation speed using the matching engine’s rate calculation, including ACTUAL, Betaflight and KISS.

Extraction and comparison now default to 30 Hz with two seconds minimum overlap. Older sessions using the former default values adopt these defaults in the controls; custom values are retained. Saved movement data at another rate is retained and labeled for re-extraction.

### StickCam extraction timing

Each calibrated StickCam clip records wall-clock time spent processing during movement-data derivation. Fresh extraction time, cache reuse lookup time, extraction rate, sample count, and failures are saved in the persistent derivation log and shown in the per-video status table. While a clip is running, its elapsed time updates with task progress. The **Whole batch** and **Current video** cards show separate percentages, elapsed times and approximate remaining times, including a local estimated batch finish time. **Progress by video** shows each queued, extracting, saving, reused, saved or failed recording. Batch progress weights uncached footage by duration × tracking Hz; a long video contributes more work than a short one. Recent successful timings from the same session, tracker version and decode mode seed estimates for similar fixed/wonky and outline tracking; current observed speed updates them after sufficient processing. Cached lookup times do not count as extraction benchmarks. Extraction stays below 100% until validation and cache saving finish. Decode/I/O, backward tracking, saving and source differences can change ETA. A failed clip is finished work, not a successful derivation, and is labelled separately. Existing running tasks can display the new cards without being interrupted; their batch queue is inferred only when available calibration and duration metadata agree with the task size.

### Prepare FPV visual motion separately

**Prepare FPV visual motion**, above Find matching flights, lists every FPV recording with duration, embedded-motion availability and saved visual-motion status. Use **Check embedded motion · all FPV** to inspect supported gyro/attitude without running optical flow. Select clips for a smaller check or derivation, or choose **Derive all without embedded motion**. Derivation checks unknown sources first, skips clips with supported gyro or quaternion-derived rates, and processes eligible clips sequentially. Parser/decoder failures appear per clip in the preparation log. No StickCam calibration is required.

Visual extraction defaults to the detected source frame rate, capped at 30 Hz, with half-rate, quarter-rate and custom-Hz options. This rate is independent of StickCam extraction. Decode is shared with the matching controls. Progress reports overall completion, per-clip elapsed time and an observed visual-extraction ETA. Cancellation keeps completed caches. Matching waits while FPV or StickCam preparation is active. The table reports samples, timeline coverage, usable tracking percentage and extraction time where recorded; older caches remain reusable even when their original time was not logged. Use **Review video + motion** to play a clip above its saved estimates, with a synchronized playhead, click-to-seek and optional following chart window. This reads the existing cache without deriving again.

Preparation, matching and chart review share compressed visual fingerprints by source identity, sampling rate and algorithm version, including legacy integer-Hz caches. Other saved rates remain on disk and are shown when detected; switch back to reuse them. The new visual fingerprint stores relative camera angular velocity and applies camera uptilt to obtain estimated body Roll/Pitch/Yaw rates in degrees per second. Old 2D flow is not reused as 3D angular rates; its cache files remain on disk. Horizontal/vertical flow and image rotation are retained as diagnostics. Recorded FOV/projection are explicit approximations, not measured lens calibration; this does not recover gravity or absolute attitude. Fast freestyle, blur and featureless sky can lose tracking; usable tracking percentage is a quality measure, not confidence that two recordings match. Embedded gyro/attitude remains preferred for automatic matching.

Session creation and pair export have no fixed recording-count limit. File size, unique filenames, available disk space and bounded worker concurrency remain validated. All-to-all matching grows with both clip counts; select a subset to avoid unnecessary work in large sessions.

### Split FPV recordings, including O4 footage

Matching can propose several independently supported FPV files for one StickCam recording, provided their aligned StickCam ranges do not substantially overlap. Each FPV file has one StickCam owner; competing/duplicate coverage remains for review. A second valid segment is not treated as a competing flight when assessing candidate margin.

After confirming a segment’s sync, **Find related FPV after saving alignment** (enabled by default) checks all session FPV filenames, including clips outside a selected matching scope. **Save & find related FPV segments** also starts this scan. The confirmed FPV offset anchors relative camera time; additional FPV filename times predict offsets into the actual StickCam duration. Dates need not be correct or match between cameras. The scan reads metadata when missing, reports progress/cancellation, and adds a **Related FPV segments** table. Preview each predicted alignment, adjust it and save it individually. Timestamp candidates do not receive an audio/motion confidence score or become confirmed automatically.

Repeated timestamps, overlapping candidates, copied/stabilised files and clock resets are flagged for review. Files without usable filename timestamps need independent audio/motion matching; file sequence numbers alone do not establish sync. Each saved segment exports its own common range and corresponding StickCam range. Batch export accepts repeated StickCam sources with distinct FPV segments, using separate output folders to preserve every export.

### Session list and collapsible workspace

Use **Delete session** beside Rename session to remove a session from the frontend list. This is reversible under **Deleted sessions · restore**. It does not delete exports, original footage, shared fingerprints or existing download links, and does not reclaim their disk space. Processing must be stopped first.

Once all source recordings are ready, **Add recordings** is hidden; use **New session** to set up another batch. Every major panel has a clickable, keyboard-accessible heading, with **Collapse all / Expand all** in the header. Explicit preview/editor/chart navigation opens its destination; routine progress updates preserve collapsed sections. Collapsing a video panel pauses its players.

Calibration view: selecting **Upside down · 180°** automatically rotates the still/reference preview by 180°. The view rotation can be toggled independently. Gimbal boxes, tip/centre clicks and shell corners map to original frame coordinates; labels remain readable. Newly marked shell corners are reordered to the source image’s corner order. View rotation does not alter saved stick inputs or apply a second axis correction; the tracker’s saved 180° setting still reverses both stick axes.

You can replace an incorrect session radio: choose the correct model or Other, select a clear frame, verify its outline/gimbals/tips, then choose **Change session radio using this frame**. The new reference gets a fresh revision. Previous orientation checks and pending automatic calibration suggestions are retired; manually confirmed calibrations remain saved for review. Finish/cancel current radio checks before changing it.

The folder picker identifies whether you are selecting **FPV video** or **StickCam video** footage. **Use this folder** stays above its scrollable file list. The larger dialog keeps its navigation/selection controls visible within the available screen height.

Session cards have lazy FPV thumbnails. The selected file is the available FPV recording closest to the mean modified date of all available recordings in that session. A still at 90 seconds is preferred, 60 seconds for shorter clips, or the midpoint if under a minute; unavailable files are skipped and up to three candidates are tried. Small JPEGs are cached on disk by source size/modified date and reused after restart. New uploads retain the browser's original modified date for selection; older uploads use their server file dates.

### Radio model shape hints

The radio dropdown includes **Other**, **Auto-detect**, and specific RadioMaster TX16S, Boxer, GX12, TX15, TX12, Pocket and Zorro, TBS Tango 2 and Mambo, and BETAFPV LiteRadio 3 families. MKI/MKII/MAX/Crush/Pro variants are identified on the appropriate family entry; HelloRadioSky and the generic Jumper option have been removed, and the previous “My session radio” option is now Other.

`fpv_matcher/radio/models.py` records manufacturer links, approximate normalized shell polygons, body proportions, gimbal landmarks/radii and display placement. The preview shows that internal geometry. These are broad detection priors, not measured engineering outlines or a trained classifier. With a marked shell, the selected model provides a small ranking prior for visible rim pairs. Without a shell or usable session image, observed gimbal rims anchor candidate body silhouettes, which need distributed image-edge support before corners can be proposed. Auto-detect lists plausible models; similar cases or covered shells remain ambiguous, and uncertain outlines/orientations require marking. The 5-second calibration scan uses these hints too and tries both upright and inverted hypotheses. Suggestions remain unconfirmed until reviewed. Hands and pinch/thumb grips can still obscure the useful edges.

An operator-confirmed session frame remains the stronger photographic reference and is attempted first. Selecting Other uses that reference and broad body-layout hints without making a model identity claim. Model selection is always a soft ranking hint; it never filters out a different visible body. The TX16S prior covers Mk1/Mk2: approximately square, gimbals in the upper half, display below. Changing the dropdown selects a pending model; **Change session radio using this frame** confirms its name and a fresh image reference. Existing manually confirmed calibrations and cached movement data are retained.

### Source recovery and drafts

After setup, **Source folders & availability** remains in the guided workspace. Check missing/changed media and relink existing filenames to a new server folder. This does not mount shares or discover additional recordings. Finish active work before relinking. Unsaved calibration drafts stay on this browser device; a changed source invalidates a draft, and **Discard draft** restores the saved calibration.

### Confirmation and exports

Separate and portrait exports require an explicitly saved, current alignment; export does not implicitly approve a rejected pair. A saved FPV owner cannot be silently displaced: select **Replace saved owner** while reviewing a different StickCam. Session deletion leaves originals and exports intact. Batch limits scale with recording count; per-clip caches survive interruption.

### Gimbal suggestions and orientation review

Suggestions search several housing sizes, consider multiple rim/body hypotheses together and compare upright/inverted body, gimbal and display layouts. **Suggest orientation from radio shape** is enabled for an unconfirmed frame; choosing Upright/180° manually disables that automatic suggestion. Ambiguous results remain warnings and require review. A symmetric outline or an obscured display cannot prove orientation. The suggest button loads its frame on demand and shows progress/errors beside the controls. Hand occlusion and clutter can still produce incorrect proposals; draw the body outline or use a clear confirmed session reference to constrain the search.

### Pilot stick assignments

**Session radio & orientation → Radio stick assignments** offers Modes 1–4, default Mode 2. The selected clip can override its mode; **Use these assignments for all StickCam clips** saves a session default and updates existing calibrations. Assignments follow the physical upright radio even when the source is inverted.

| Mode | Left horizontal | Left vertical | Right horizontal | Right vertical |
| --- | --- | --- | --- | --- |
| 1 | Yaw | Pitch | Roll | Throttle |
| 2 (default) | Yaw | Throttle | Roll | Pitch |
| 3 | Roll | Pitch | Yaw | Throttle |
| 4 | Roll | Throttle | Yaw | Pitch |

Fingerprints retain canonical Mode 2 and physical stick observations. Loading them maps to the selected pilot mode, including observation quality and axis signs, without another video scan. Matching and saved review signals use named Roll/Pitch/Yaw/Throttle channels; charts record the mode used. Rate curves convert the three rotation commands to requested degrees per second; throttle stays normalized stick position and is not equated to gyro or vertical image flow. Reopen old charts / rerun motion matching after changing assignments.

### Experimental visual angular rates

Visual preparation now uses tracked viewing rays: robust rotation-only fitting for low-parallax intervals, essential-matrix pose recovery for translation, and rejection of unsupported/planar/poorly distributed intervals. Local rotation vectors avoid Euler wrapping during inversion. The camera basis is right/down/forward, mapped to body forward/right/down and corrected by the configured camera uptilt. Recorded horizontal FOV and rectilinear/equidistant projection are saved per session and form part of the cache identity; the initial 110° is explicitly an approximation. Choose settings matching the recorded video crop and projection.

The video review plots estimated Roll/Pitch/Yaw below playback. Matching uses those same body-axis sequences against rates-derived StickCam requests, and continues to prefer supported embedded telemetry. Quality gaps remain gaps. This estimator has not yet been evaluated against labelled freestyle gyro recordings; unknown lens distortion, stabilization, parallax, blur and sampling gaps remain limits. Native gyro and estimated rotation are labelled separately. Relative rotation alone cannot measure throttle, gravity or absolute attitude.

Saved visual-motion playback validates its camera-to-body mapping as the proper Z/X/Y rotation. Metadata/chart reads have a 30-second timeout and failures appear beside the preview, with chart loading separated from video loading. Playback and chart windows read saved data without starting fresh extraction.
