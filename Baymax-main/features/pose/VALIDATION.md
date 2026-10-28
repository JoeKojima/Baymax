# Pose port validation (ONNX Runtime on macOS; the NPU on the Orange Pi)

Does `features/pose` (the code path the NPU backend runs) behave like MediaPipe's
PoseLandmarker? Everything up to "On the Orange Pi (NPU)" is the port on ONNX Runtime
(FP32) on a Mac: run 2026-10-27 on an Apple M1 (macOS 26.5.2, Python 3.12.13,
mediapipe 0.10.35, onnxruntime 1.31.1, opencv-contrib-python 5.0.0.93). Its results
(including "same fall decisions") are not the NPU's: the NPU (FP16) on the Orange Pi
was measured separately and differs more; see "On the Orange Pi (NPU)" below. Re-run the same day after the
review fixes (NPU fallback on any probe error, NaN guard, stronger converter check):
every accuracy statistic below was identical (896 per-clip/per-sequence statistics
compared); that run's timings are not used, the machine was busy. Run a third time after
the second review's fixes (visibility gates in the converter check, the runtime's
confidences passed to the port): every accuracy value in the output JSON identical
again (timings excluded, not used).

## Models

ONNX exported by `convert_rknn.py export-onnx` from the runtime's
`pose_landmarker_full.task` (TFLite SHA-256s as pinned in `models.py`):
`pose_detector.onnx` 76bb41e7…c9ce, `pose_landmarks_detector.onnx` 93f3dde5…8a2e
(TensorFlow 2.21.0, tf2onnx 1.17.0, onnx 1.23.2). Worst |ONNX - TFLite| over random and
real inputs: detector boxes 0.0005 (values up to 212), scores 0.0003; landmarks 0.0018 px
(up to 178), pose flag 8e-6, heatmap 0.0004. ONNX Runtime, the TFLite interpreter with
and without XNNPACK give the same pipeline output, so the engine is not a source of
difference. OpenCV Zoo's 2023mar ONNX models differ from the .task's (up to 38 px on the
same input) and were not used.

## Method

`python -m features.pose.validate` (reproduce with the commands at the end). Each clip
is centre-cropped to 4:3, resized to 640x480 like the robot camera, sampled at 15 fps
(the runtime's pose rate) with the clip's own timestamps; at most 60 s per clip. Three
runs see identical frames:

- **MediaPipe**: PoseLandmarker VIDEO mode with the runtime's options (reference).
- **port**: `features/pose` pipeline on ONNX Runtime.
- **baseline**: MediaPipe again with ±1 grey level of random noise added: how much
  MediaPipe itself changes for an invisible input change. The port is judged against it.

Error = Euclidean distance of normalised (x, y) to the reference, on frames where both
found a pose. Face presence = landmarks 0, 2, 5 all with visibility >= 0.5 (the
runtime's rule). Fall decisions = `FallDetector` with the runtime's settings (60°,
25°/s, 0.12/s, 2 frames, 3 s cooldown, 2 s stay-down) on a simulated clock.

Clips (Wikimedia Commons 480p transcodes, kept out of the repo):

| Clip | Content | Licence |
|---|---|---|
| gait_elderly | older adults walking on a treadmill, mostly legs in view | CC BY 2.0, van Ooijen et al., BMC Geriatrics 13:34 |
| sign_seated | one person standing full-body, signing, next to a bicycle | CC0 |
| interview_deryck | seated profile interview, upper body | CC BY 4.0, Wikimedia Deutschland |
| interview_odia | face/shoulders close-up interview | CC BY-SA 4.0, Subhashish Panigrahi |
| kettlebell_squat | squats, side view | CC BY-SA 4.0, Taco fleur |
| squat_demo | squats, back view (7 s) | CC BY 3.0, FitnessScape |
| deadlift_bend | soldiers deadlifting, several people in view | Public domain (U.S. Army) |
| pushup_prone | standing -> squat -> prone on the floor -> standing | Public domain (U.S. Army) |

Synthetic falls: from sign_seated, kettlebell_squat and pushup_prone at 0 s and 25 s,
1.5 s upright, then the live frames rotated 90° about the ankles (from MediaPipe's
first frame) in four scenarios: fast fall left (0.4 s), fast fall right (0.6 s), slow
lie-down (4 s), fall and get up within 1 s; 24 sequences, 2,130 frames.

## Results: clips (5,945 frames)

| | port vs MediaPipe | MediaPipe+noise vs MediaPipe |
|---|---|---|
| pose found (MediaPipe: 4,850) | 5,020 | 5,020 |
| detection agreement | 96.64% | 96.47% |
| … without the legs-only treadmill clip (5,044 frames) | 99.48% | 99.27% |
| landmark 0 (nose) error median / p95 | 0.0016 / 0.0215 | 0.0017 / 0.0263 |
| landmark 2 (left eye) | 0.0015 / 0.0207 | 0.0016 / 0.0238 |
| landmark 5 (right eye) | 0.0015 / 0.0197 | 0.0016 / 0.0238 |
| landmark 11 (left shoulder) | 0.0017 / 0.0270 | 0.0018 / 0.0325 |
| landmark 12 (right shoulder) | 0.0018 / 0.0226 | 0.0018 / 0.0297 |
| landmark 23 (left hip) | 0.0016 / 0.0453 | 0.0018 / 0.0593 |
| landmark 24 (right hip) | 0.0018 / 0.0429 | 0.0019 / 0.0569 |
| visibility agreement at 0.5, landmarks 0 / 2 / 5 | 100 / 100 / 100% | 100 / 100 / 100% |
| visibility agreement at 0.5, landmarks 11 / 12 / 23 / 24 | 99.88 / 98.74 / 99.50 / 99.54% | 99.88 / 98.80 / 98.80 / 98.76% |
| face presence agreement (all frames) | 96.64% (100% where both found a pose) | 96.47% |
| trunk angle abs difference median / p95 | 0.14° / 1.30° | 0.15° / 1.33° |
| fall events (MediaPipe: 2) | 2, same frames | 2, same frames |
| fall-pending frame agreement | 99.55% | 99.60% |

(0.001 normalised is 0.64 px horizontally at 640x480.)

Per clip:

| Clip | Frames | MediaPipe found | Detection agreement port / baseline | Shoulders+hips error median (p95), port | baseline | Falls MediaPipe / port / baseline |
|---|---|---|---|---|---|---|
| gait_elderly | 901 | 214 | 80.7% / 80.8% | 0.0248 (0.0767) | 0.0282 (0.0828) | 0 / 0 / 0 |
| sign_seated | 901 | 901 | 99.9% / 100% | 0.0005 (0.0054) | 0.0006 (0.0051) | 1 / 1 / 1 |
| interview_deryck | 751 | 711 | 98.5% / 97.5% | 0.0067 (0.1352) | 0.0069 (0.3861) | 0 / 0 / 0 |
| interview_odia | 721 | 601 | 98.6% / 99.6% | 0.0126 (0.0629) | 0.0136 (0.0671) | 0 / 0 / 0 |
| kettlebell_squat | 762 | 762 | 100% / 100% | 0.0006 (0.0031) | 0.0007 (0.0033) | 0 / 0 / 0 |
| squat_demo | 107 | 107 | 100% / 100% | 0.0008 (0.0027) | 0.0008 (0.0025) | 0 / 0 / 0 |
| deadlift_bend | 901 | 659 | 99.6% / 98.3% | 0.0019 (0.0270) | 0.0020 (0.1344) | 0 / 0 / 0 |
| pushup_prone | 901 | 895 | 100% / 100% | 0.0013 (0.0071) | 0.0014 (0.0069) | 1 / 1 / 1 |

Where only the legs (treadmill) or a face close-up are in view, MediaPipe's own output
is unstable: an invisible change to the input moves it as much as the port does, and
whether it keeps tracking flips (MediaPipe found a pose in 214 treadmill frames; with
noise, 387; the port, 388). With a whole person in view the port is within about one
pixel of MediaPipe.

## Results: synthetic falls (24 sequences, 2,130 frames)

| | port vs MediaPipe | MediaPipe+noise vs MediaPipe |
|---|---|---|
| sequences with the same fall frames | **24 / 24** | 23 / 24 |
| fall events (MediaPipe: 11) | 11 | 11 |
| fall-pending frame agreement | 99.86% | 98.69% |
| detection agreement | 96.24% | 95.40% |
| shoulder/hip error median / p95 | 0.0007-0.0009 / 0.015-0.032 | 0.0011-0.0013 / 0.017-0.026 |
| visibility agreement at 0.5 (0, 2, 5, 11, 12, 23, 24) | 100% | 100% |
| trunk angle abs difference median / p95 | 0.21° / 2.51° | 0.23° / 2.51° |

Every sequence where MediaPipe confirmed a fall, the port confirmed it on the same frame;
no sequence had a fall in one and not the other. (With the runtime's thresholds,
`FallDetector` also flags the 4 s "slow lie-down" in MediaPipe and in the port: the hips
drop faster than 0.12 frame-heights/s. That is the detector's existing tuning, unchanged
here.) Several upright starts never reach a fall in either because the person was not
fully upright or in view at that moment.

## Single frames and smoothing

- First frame only (detector path, no smoothing), 57 frames sampled from all clips, key
  landmarks 0/2/5/11/12/23/24: port vs MediaPipe median 0.00044, p95 0.0038, max 0.055;
  MediaPipe vs MediaPipe+noise median 0.0018, p95 0.0126, max 0.059. The largest
  differences are close-ups where the predicted body ROI is several times the frame.
- Found / not found per frame: identical on kettlebell_squat, squat_demo and
  pushup_prone, one frame different on sign_seated (901 frames).
- Visibility smoothing across a lost pose (pose, 3 empty frames, partial pose, 2 empty
  frames, pose): MediaPipe keeps the visibility filter's state through the gaps
  (an occluded knee reads 0.91, 0.84, 0.77 … instead of dropping to 0.18);
  the port reproduces this to ±0.004, a variant that resets would not.

## The CPU path is unchanged

`create_pose_backend(mediapipe_options=options)` off the RK3588 returns MediaPipe making the
camera thread's former calls: on 301 frames of sign_seated its landmarks were
bit-identical (x, y, z, visibility, presence) to calling `PoseLandmarker.detect_for_video`
directly as `realtime_gemini_10.py` did before.

## MediaPipe 0.10.18 (the Orange Pi's pin)

With numpy 1.26.4, opencv-contrib-python 4.10.0.84 and mediapipe 0.10.18 (the versions in
requirements-orange-pi.txt, macOS wheels): the offline tests and the parity test pass,
and on 20 s each of kettlebell_squat, sign_seated and pushup_prone (903 frames)
detection agrees on 100% of frames, key-landmark error median 0.0004-0.0006 / p95
0.0025-0.0041 (MediaPipe+noise: 0.0006-0.0008 / 0.0028-0.0038), visibility agreement 100%.

## CPU time per frame (Apple M1, quiet machine, 640x480, 300 frames)

| | median | p90 |
|---|---|---|
| MediaPipe CPU (runtime calls) | 25.7 ms | 26.4 ms |
| port on ONNX Runtime | 24.8 ms | 29.7 ms |
| of which ONNX inference | detector 20.7 ms (when not tracking), landmarks 21.2 ms | |
| port pre/post-processing (warps, decode, refinement, smoothing) | 0.57 ms | |

On the board the two inferences move to the NPU, yet the process still used 8-9 ms of
CPU per NPU frame with a person in view and about 13 ms with nobody in view, paced at
15 fps (see "On the Orange Pi"; how that splits between the numpy/OpenCV pipeline and
librknnrt's own work inside each inference call was not measured). These
Mac numbers say nothing about RK3588 speed; use `benchmark.py` on the board.

## The converter's accuracy check, with an FP16 stand-in for the NPU

The NPU cannot run here, so `convert_rknn.py check` (the gate for using the NPU) ran on
the Mac with a stand-in for `RknnEngine`: ONNX Runtime on copies of the ONNX models in
which the normalised input, every float activation and the outputs are rounded to
float16 (arithmetic in float32). This approximates FP16 storage on the NPU; it is
**not** the NPU, whose operator implementations, fusion and accumulation may differ
more. Default check = the sample image (3 stills) + the 72-frame tracked sequence
(sway, person gone for 3 frames, 90° fall in 0.8 s, lying); `--video` adds 300 frames
of a validation clip.

| | default | + pushup_prone | + sign_seated | + kettlebell_squat | limit |
|---|---|---|---|---|---|
| frames (pose in both / both tracking) | 75 (72 / 67) | 375 (361 / 355) | 375 (372 / 366) | 375 (372 / 366) | >= 50 / >= 30 |
| raw landmark x/y 0-34, max (256-px crop) | 0.74 | 0.74 | 0.80 | 0.91 | <= 3 |
| of which 33/34 (tracking) | 0.30 | 0.30 | 0.32 | 0.34 | |
| raw visibility 0-32 (after sigmoid), max | 0.0088 | 0.0177 | 0.0170 | 0.0139 | <= 0.05 |
| raw presence, max | 0.017 | 0.017 | 0.017 | 0.017 | (recorded) |
| raw pose flag, max | 0.0003 | 0.0004 | 0.0003 | 0.004 | <= 0.05 |
| detection agreement | 100% | 100% | 100% | 100% | >= 98% |
| landmark error p95 / p99 (normalised) | 0.0020 / 0.0086 | 0.0032 / 0.0117 | 0.0020 / 0.0043 | 0.0041 / 0.0082 | <= 0.01 / 0.03 |
| next-frame ROI centre, max distance | 0.0028 | 0.0071 | 0.0032 | 0.0048 | (recorded) |
| visibility agreement at 0.5, landmarks 0-32 | 99.96% | 99.94% | 99.99% | 99.83% | >= 98% |
| of which 0, 2, 5, 11, 12, 23, 24 (each) | 100% | 100% | 100% | 100% | (recorded) |
| smoothed visibility difference, max | 0.018 | 0.030 | 0.023 | 0.031 | (recorded) |
| result | passed | passed | passed | passed | |

Fault injection, default check:

| NPU stand-in | fails on | passed before |
|---|---|---|
| ONNX exact, landmarks 33/34 (place the next ROI) 4 px off in x | raw x/y 4.0 > 3 | first version (0-32 on stills only) |
| ONNX exact, 33/34 12 px off | raw x/y, and the tracked pipeline's p99 0.031 > 0.03 | first version |
| FP16 stand-in, visibility logits of 11/12/23/24 negated (FallDetector would skip every frame) | raw visibility 0.9999 > 0.05; agreement 87.8% < 98% (11/12/23/24: 0%) | second version (visibility recorded, not gated) |
| FP16 stand-in, every visibility logit 0.5 too low | raw visibility 0.13 > 0.05 (agreement 99.45% alone would pass) | second version |
| FP16 stand-in, every visibility logit 1.0 / 2.0 too low | raw visibility 0.25 / 0.47; at 2.0 also agreement 96.7% | second version |

The x/y statistics above are identical to those of the second version's run, so adding
the visibility gates changed nothing else. Offline tests cover the other failure paths
(pose flag, nothing compared, NaN outputs, `init_runtime` errors).

## On the Orange Pi (NPU)

Run 2026-10-28 on an Orange Pi 5 Max: Armbian Debian 13, kernel 6.1.172-vendor-rk35xx,
RKNPU 0.9.8, librknnrt / rknn-toolkit2 / rknn-toolkit-lite2 2.3.2, MediaPipe 0.10.18 (the
Pi profile), app pinned to the A76 cores. The converter ran on the board
(`setup_converter.sh` + `convert_rknn.py`: ONNX from the runtime's .task, FP16 RKNN,
check on NPU core 0) and its accuracy check passed: detection agreement 100%,
landmark error median 0.00016 / p95 0.00099 / p99 0.0048, visibility agreement 99.96%
(100% for 0, 2, 5, 11, 12, 23, 24), against the ONNX port.

**Speed and CPU** (NPU core 0, app pinned to the A76 cores; `benchmark.py`, the paced
rows with the code at commit 61c410e):

| | CPU MediaPipe (A76) | NPU |
|---|---|---|
| ms per frame back to back, per-clip median (`--video`, up to 300 frames of each of the 17 board clips) | 36-44 | 10.5-11.9 |
| detector / landmark network on the NPU, back to back | | 10.1 ms / 8.0 ms |
| ms per frame paced at 15 fps, person in view (`--fps 15`): median (p90, p99) | 43.7 | 16.2 (23.8, 32.7) |
| detector / landmark network on the NPU, paced, person in view | | 11.4 ms (1 run) / 12.6 ms (300 runs) |
| process CPU paced at 15 fps, person in view | 76.7% of a core (51.1 ms/frame) | 12.4% of a core (8.3 ms/frame) |
| ms per frame paced at 15 fps, nobody in view (`--fps 15 --empty`): median | 39.6 | 28.5 |
| detector on the NPU, paced, nobody in view (it runs every frame) | | 21.6 ms (about 10 ms back to back) |
| process CPU paced at 15 fps, nobody in view | 70.5% of a core (47.0 ms/frame) | 19.7% of a core (13.1 ms/frame) |

How these were measured. By default `benchmark.py` runs frames back to back, which
keeps the CPU and the NPU clocked up (the board's NPU devfreq governor is
`rknpu_ondemand`, the A76s' `ondemand`): the first two rows are throughput. Ember paces
frames at 15 fps, so the clocks drop between frames and an NPU frame takes longer;
`--fps 15` paces frames the same way (each frame starts in its own 1/15 s slot), and
that paced time is what the fall toolkit's Monitor shows. MediaPipe's time barely
changes with pacing. With a person in view (the benchmark's swaying sample image, 300
frames of which 20 warm-up) the detector ran once and the landmark network on every
frame; the NPU and MediaPipe found a pose on the same 280 of 280 measured frames,
landmark error median 0.0002, p95 0.0010 (normalised). With nobody in view (`--empty`,
an empty noisy frame) the detector runs on every frame, and paced, one run took 21.6 ms
against about 10 ms back to back. Process CPU is the CPU time of all the process's
threads while each frame's `detect()` runs, as a share of one core at 15 fps; CPU used
between frames is not counted. Earlier measurements that differ in method agree with
it: a small paced script that divided all process CPU over a 20 s run by wall time (the
backend from `create_pose_backend`, `BAYMAX_POSE_BACKEND=cpu` then auto = NPU, a still
photo of a person shifted 2 px every other frame) gave 13% for the NPU and 76% for
MediaPipe, and two re-measurements during review (the sample image, an empty noisy
frame; 150-300 frames each) gave about 16 ms (p90 23-29) with a person in view, and
17-29 ms and 14-23% of a core with nobody in view. On the A55 cores MediaPipe took
215-247 ms per frame (back to back), which is why Ember now runs on the A76s. The ONNX
Runtime port on the board's CPU (development only, not used by Ember there) used about
three cores.

**NPU against MediaPipe, Ember-level** (`python -m features.pose.validate --engine npu`,
the 17 board clips below, 20 s each at 15 fps, plus the synthetic falls; "noise" is
MediaPipe against itself with +-1 grey level):

| | NPU vs MediaPipe | noise vs MediaPipe |
|---|---|---|
| real clips: detection agreement (4,303 frames) | 98.1% | 95.5% |
| real clips: face presence agreement | 98.1% | 95.3% |
| real clips: landmark p95, 0/2/5/11/12/23/24 | 0.045-0.076 | 0.078-0.166 |
| real clips: visibility agreement, 11/12/23/24 | 99.8 / 100 / 97.4 / 97.4% | 99.6 / 100 / 97.6 / 97.6% |
| real clips: trunk angle difference median / p95 | 0.12° / 1.93° | 0.15° / 3.58° |
| real clips: fall-pending frames agreement | 99.70% | 99.70% |
| real clips: falls (MediaPipe: 2) | 1 | 2 |
| synthetic: detection agreement (2,130 frames) | 96.9% | 99.0% |
| synthetic: falls (MediaPipe: 11) | 10 | 11 |
| synthetic: fall-pending frames agreement | 98.4% | 99.1% |

So on real clips the NPU is closer to MediaPipe than MediaPipe is to itself, but on
the synthetic falls it is below that baseline, and it differed on 2 of MediaPipe's 13
fall decisions: it confirmed 1 of MediaPipe's 2 real-clip falls and 10 of its 11
synthetic ones. Unlike the ONNX port on the Mac (same fall decisions), the NPU does not
reproduce MediaPipe's fall decisions exactly. The two differences, one a fall MediaPipe
confirmed and the NPU dropped, the other a "no fall" sequence MediaPipe flagged and the
NPU did not:

- **judo_soft** (two people): both engines flag the throw on the same frames (suspicious
  at frame 67, pending from 68). The judoka gets up at once; MediaPipe kept the trunk at
  48-68° through the get-up, so the 2 s stay-down ran out at frame 92 and it confirmed a
  fall. The NPU lost the person for one frame (80), re-detected the standing partner
  (judging by the jump from 75° to 44°), read the trunk as upright and dropped the
  pending fall: the onset matched, but a fall MediaPipe confirmed was dropped. Known
  limit: with two people in view (`num_poses=1`) either engine can lock onto the other
  person, and the NPU loses and re-finds the person more often than MediaPipe, so it has
  more chances to; that is the case of someone standing next to a person who fell.
- **kettlebell_squat at 25 s, slow lie-down**: a sequence labelled "no fall" (a slow
  4 s lie-down); MediaPipe flagged it, the NPU didn't. The other slow lie-downs match
  MediaPipe to the frame (one is a frame earlier).

Against the ONNX port on the same real clips, the FP16 NPU differs more than the
converter's synthetic check shows (`check --video` on the 8 hard clips below: landmark
p95 0.016-0.28, detection agreement 94-100%, visibility of 0/2/5/11/12 100% and 23/24
98.6-100%): on real footage the NPU loses and re-finds the person a little more often,
mainly in unusual poses (rotated, upside down) and with two people in view. The
converter's default gate uses only its stills and synthetic sequence and catches broken
conversions; it says nothing about real footage. With `--video` the clip is gated too,
with limits set for the synthetic sequence, and every one of these 8 real clips missed
them (p95 over 0.01), so a check with `--video` keeps Ember on the CPU (a plain `check`
restores the NPU). To judge the NPU on real footage, compare it with MediaPipe with
`validate.py --engine npu`, as the table above does. The same MediaPipe port on ONNX
Runtime (FP32) on the board matched MediaPipe 0.10.18 at least as well as MediaPipe
matches itself (8 hard clips: detection 97.0% vs 95.3%, face presence 97.0% vs 94.2%,
fall-pending 100%).

Board clip sets (Wikimedia Commons, kept out of the repo like the Mac set):

- **17 board clips** (the benchmark rows and the NPU-vs-MediaPipe table): the 8 Mac
  clips above (gait_elderly, sign_seated, interview_deryck, interview_odia,
  kettlebell_squat, squat_demo, deadlift_bend, pushup_prone) and chair_stand,
  chair_yoga, stand_to_sit, tug, balance4, cpr_training, judo_okuri, judo_soft,
  judo_newsreel.
- **8 hard clips** (ONNX on the board, `check --video`): chair_stand, interview_odia,
  interview_deryck, gait_elderly, tug, cpr_training, chair_yoga, stand_to_sit.

The nine board-only clips (480p transcodes from upload.wikimedia.org of these Commons
files, `https://commons.wikimedia.org/wiki/File:<file>`; the Mac clips' are in the
tables above and below):

| Clip | Commons file | Licence | SHA-256 |
|---|---|---|---|
| balance4 | 4-Stage_Balance_Test.webm | Public domain (CDC) | c42baead…2478 |
| chair_stand | 30-Second_Chair_Stand_Test.webm | Public domain (CDC) | 04987cfc…126d |
| chair_yoga | Chair_Yoga_for_Beginners.ogv | CC BY-SA 3.0, Nobody60 | cf9118f5…5c0e |
| cpr_training | Save_a_Life_Cymru_-_Training_video.webm | CC BY 3.0, Welsh Government | bdf0cec3…3260 |
| judo_newsreel | Judo_voor_huisvrouwen-510080.ogv | CC BY-SA 3.0 NL, Polygoon-Profilti / Beeld en Geluid | 65101837…d2ef |
| judo_okuri | JudoVideo.net_Okuri-ashi-barai.webm | CC BY 3.0, canaljudovideo | aaf704af…4287 |
| judo_soft | D1._Dobások_lágy_stílussal_1,00.webm | CC0, Hvja | 9d120a89…f586 |
| stand_to_sit | Implementation-of-expert-systems-to-support-the-functional-evaluation-of-stand-to-sit-activity-1475-925X-13-98-S3.ogv | CC BY 2.0, Junkes-Cunha et al. | 825bcc82…3716 |
| tug | The_Timed_Up_and_Go_(TUG)_Test.webm | Public domain (CDC) | d136a8e5…1398 |

The SHA-256s of all 17 board clips were confirmed on the board (the 8 Mac clips' match
the Mac copies).

## Not covered

- Ember itself running with the NPU backend end to end: the live camera thread,
  Monitor `Running on NPU (core pose)`, a mid-run fallback to MediaPipe, a conversation
  alongside, and thermals over a long run (only the standalone tools above ran on the
  board, with Ember stopped; the backend choice was confirmed by calling
  `create_pose_backend` there).
- The camera loop with the 1-frame V4L2 buffer on the Orange Pi's camera (Linux ARM64
  only, `pc_hardware.latest_frames_only`): camera loop FPS and frame age were not
  measured with a real camera on any platform (the Monitor's FPS should stay near 15;
  the ~0.2 s saved is an estimate from OpenCV's four-frame queue).
- Fall accuracy at home: real falls; older adults with Ember's camera position,
  lighting and occlusions; more than one person (num_poses=1, as in the runtime; see
  judo_soft above).
- Boards, images and toolkit versions other than the one above (the converter's gate
  checks the conversion there, not real footage: run `validate.py --engine npu`).

## Reproduce

```bash
python -m features.pose.validate --task pose_landmarker_full.task --onnx-dir .models/pose_npu \
  --fps 15 --seconds 60 gait_elderly.webm sign_seated.webm interview_deryck.webm \
  interview_odia.webm kettlebell_squat.webm squat_demo.webm deadlift_bend.webm pushup_prone.webm \
  --falls sign_seated.webm kettlebell_squat.webm pushup_prone.webm --fall-starts 0,25 --json out.json
python features/pose/benchmark.py --task pose_landmarker_full.task --video sign_seated.webm
# on the Orange Pi (Ember stopped), after convert_rknn.py: the same comparison with the NPU as the port
taskset -c 4-7 .venv/bin/python -m features.pose.validate --engine npu --task pose_landmarker_full.task \
  --onnx-dir .models/pose_npu --fps 15 --seconds 20 gait_elderly.webm sign_seated.webm \
  interview_deryck.webm interview_odia.webm kettlebell_squat.webm squat_demo.webm deadlift_bend.webm \
  pushup_prone.webm chair_stand.webm chair_yoga.webm stand_to_sit.webm tug.webm balance4.webm \
  cpr_training.webm judo_okuri.webm judo_soft.webm judo_newsreel.webm \
  --falls sign_seated.webm kettlebell_squat.webm pushup_prone.webm --fall-starts 0,25 --json npu.json
# back to back per clip (the first two speed rows), then paced like Ember (the other rows)
for clip in *.webm; do .venv/bin/python features/pose/benchmark.py --video "$clip" --frames 300; done
.venv/bin/python features/pose/benchmark.py --fps 15
.venv/bin/python features/pose/benchmark.py --fps 15 --empty
# the ONNX port on the board (needs onnxruntime in .venv), and NPU vs ONNX per hard clip (back
# up manifest.json first: a --video check that misses its limits records passed=false)
taskset -c 4-7 .venv/bin/python -m features.pose.validate --task pose_landmarker_full.task \
  --onnx-dir .models/pose_npu --fps 15 --seconds 20 chair_stand.webm interview_odia.webm \
  interview_deryck.webm gait_elderly.webm tug.webm cpr_training.webm chair_yoga.webm stand_to_sit.webm
for clip in chair_stand interview_odia interview_deryck gait_elderly tug cpr_training chair_yoga stand_to_sit; do
  .models/pose_npu/build/venv-rknn/bin/python features/pose/convert_rknn.py check --video $clip.webm --max-frames 300
done
```

Clip SHA-256 (480p VP9 transcodes from upload.wikimedia.org): gait_elderly 10492f57…deef,
sign_seated 1fdf9e5a…6ec26, interview_deryck 1cf87194…be12b, interview_odia 3821a238…7c01,
kettlebell_squat 2bd86478…5e2e, squat_demo 883b0f88…582e, deadlift_bend 3d6993e5…4c0f,
pushup_prone 338d26cc…2ed9.
