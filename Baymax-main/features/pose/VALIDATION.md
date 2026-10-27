# Pose port validation (macOS, ONNX Runtime engine)

Does `features/pose` (the code path the NPU backend runs) behave like MediaPipe's
PoseLandmarker? Run 2026-10-27 on an Apple M1 (macOS 26.5.2, Python 3.12.13,
mediapipe 0.10.35, onnxruntime 1.31.1, opencv-contrib-python 5.0.0.93). The NPU
itself has **not** been measured yet: RKNN accuracy and speed come from
`convert_rknn.py` / `benchmark.py` on the Orange Pi. Re-run the same day after the
review fixes (NPU fallback on any probe error, NaN guard, stronger converter check):
every accuracy statistic below was identical (896 per-clip/per-sequence statistics
compared); that run's timings are not used, the machine was busy.

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

On the board the two inferences move to the NPU; only the ~0.6 ms of numpy/OpenCV work
(more on an A76) stays on the CPU. These Mac numbers say nothing about RK3588 speed; use
`benchmark.py` on the board.

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
| raw pose flag, max | 0.0003 | 0.0004 | 0.0003 | 0.004 | <= 0.05 |
| detection agreement | 100% | 100% | 100% | 100% | >= 98% |
| landmark error p95 / p99 (normalised) | 0.0020 / 0.0086 | 0.0032 / 0.0117 | 0.0020 / 0.0043 | 0.0041 / 0.0082 | <= 0.01 / 0.03 |
| next-frame ROI centre, max distance | 0.0028 | 0.0071 | 0.0032 | 0.0048 | (recorded) |
| visibility agreement at 0.5 | 99.96% | 99.94% | 99.99% | 99.83% | (recorded) |
| result | passed | passed | passed | passed | |

Fault injection (ONNX exact except landmarks 33/34, which place the next ROI, shifted in
x): by 4 px, the check fails on the raw comparison (4.0 > 3); by 12 px it also fails on
the tracked pipeline (p99 0.031). The first version of the check, which compared
landmarks 0-32 on three still frames only, passed both. Offline tests cover the other
failure paths (pose flag, nothing compared, NaN outputs, `init_runtime` errors).

## Not covered

RKNN conversion and FP16 NPU accuracy/speed on the NPU itself (board only;
`convert_rknn.py check` gates use of the NPU; above is only a stand-in); a Linux aarch64
run of these tests; real falls; older adults at
home with Ember's camera position, lighting and occlusions; more than one person (num_poses=1,
as in the runtime).

## Reproduce

```bash
python -m features.pose.validate --task pose_landmarker_full.task --onnx-dir .models/pose_npu \
  --fps 15 --seconds 60 gait_elderly.webm sign_seated.webm interview_deryck.webm \
  interview_odia.webm kettlebell_squat.webm squat_demo.webm deadlift_bend.webm pushup_prone.webm \
  --falls sign_seated.webm kettlebell_squat.webm pushup_prone.webm --fall-starts 0,25 --json out.json
python features/pose/benchmark.py --task pose_landmarker_full.task --video sign_seated.webm
```

Clip SHA-256 (480p VP9 transcodes from upload.wikimedia.org): gait_elderly 10492f57…deef,
sign_seated 1fdf9e5a…6ec26, interview_deryck 1cf87194…be12b, interview_odia 3821a238…7c01,
kettlebell_squat 2bd86478…5e2e, squat_demo 883b0f88…582e, deadlift_bend 3d6993e5…4c0f,
pushup_prone 338d26cc…2ed9.
