# Pose on the Orange Pi NPU (features/pose)

The core camera thread's pose estimate (fall detection, face presence for
conversation initiation, the live-view skeleton and the fall toolkit) can run on
the RK3588's NPU instead of MediaPipe on the CPU. Everywhere else, and on any
RK3588 where the NPU is not ready, it is MediaPipe on the CPU exactly as before.

## Inputs, outputs, enabling

`realtime_gemini_10.py` calls `create_pose_backend(mediapipe_options=options)` once
when the camera thread starts, then `pose.detect(frame_bgr, timestamp_ms)` per frame.
Output is `None` or 33 landmarks with MediaPipe's `x, y, z, visibility, presence`
(same conventions as `NormalizedLandmark`), so `FallDetector`, face presence, the
skeleton drawing and the fall toolkit are unchanged. `pose.name` is
`NPU (RK3588)` or `CPU (MediaPipe)`; `pose.device` (`NPU`/`CPU`) is shown in the fall
toolkit status as `Running on NPU (core pose)`.

| `BAYMAX_POSE_BACKEND` | Behaviour |
|---|---|
| `auto` (default) | NPU only if **all** hold: Linux aarch64; `rockchip,rk3588` in `/proc/device-tree/compatible`; `rknnlite` (rknn-toolkit-lite2) imports; `.models/pose_npu/*.rknn` exist with the SHA-256s in `manifest.json`; the converter's on-board accuracy check passed; `init_runtime` and a warm-up inference succeed. Otherwise MediaPipe CPU. |
| `cpu` | MediaPipe CPU, no probing. |
| `npu` | Skips the board and accuracy-check gates (other RK3588 compatibles, testing a model that failed the check); still needs rknnlite, matching files and a working runtime. Falls back to CPU if they are missing. |
| `onnx` | The same port on ONNX Runtime on the CPU (development; needs `onnxruntime`). |

`BAYMAX_POSE_NPU_CORE` = `0` (default), `1`, `2`, `0_1`, `0_1_2` or `auto` (RKNNLite
core mask). `BAYMAX_POSE_MODEL_DIR` moves the model directory (default
`Baymax-main/.models/pose_npu`, ignored by Git and kept across updater checkouts).

The choice is printed once, e.g. `[POSE] Pose backend: CPU (MediaPipe). NPU skipped:
not Linux aarch64 (darwin arm64).` or `[POSE] Pose backend: NPU (RK3588) on NPU core 0
(models from rknn-toolkit2 2.3.2)`. If the NPU raises during use the backend logs it
and switches to MediaPipe for the rest of the run.

## Files

| File | Role |
|---|---|
| `backend.py` | Backend choice, the MediaPipe CPU backend (the camera thread's former calls), NPU-to-CPU fallback. |
| `pipeline.py` | MediaPipe Pose Landmarker VIDEO-mode graph in numpy + OpenCV. |
| `engines.py` | `OnnxEngine` (ONNX Runtime CPU) and `RknnEngine` (`rknnlite`, imported only here, lazily). |
| `models.py` | Model directory, pinned SHA-256s, manifest. Standard library only. |
| `convert_rknn.py`, `setup_converter.sh` | Board-side conversion (below). |
| `benchmark.py` | Per-frame ms: MediaPipe CPU vs NPU (and ONNX Runtime if installed). |
| `validate.py` | Development: port vs MediaPipe on video clips; numbers in [VALIDATION.md](VALIDATION.md). |

## The models

Both networks come from the same `pose_landmarker_full.task` (float16) the runtime
uses, so CPU and NPU share weights. The converter checks the SHA-256 of the two
TFLite files inside the bundle (`models.TFLITE_SHA256`; version 1 and `latest` of the
.task are different zips of identical TFLite files):

| Network | Input | Normalisation (MediaPipe ImageToTensor) | Outputs kept |
|---|---|---|---|
| `pose_detector` | 224x224 RGB, the whole frame letterboxed (black bars) | `(x - 127.5) / 127.5` -> [-1, 1] | `Identity` [1,2254,12] boxes+4 keypoints, `Identity_1` [1,2254,1] score logits |
| `pose_landmarks_detector` | 256x256 RGB, rotated ROI, edge pixels replicated | `x / 255` -> [0, 1] | `Identity` [1,195] = 39 x (x, y, z, visibility, presence), `Identity_1` [1,1] pose flag (already a probability), `Identity_3` [1,64,64,39] heatmap |

The segmentation mask and world landmarks are dropped (about a third of the landmark
model's compute). Conversion: the detector stores 38 weight tensors sparse (`DENSIFY`
ops), which tf2onnx cannot read, so the converter first replaces them with their dense
values (computed by the TFLite interpreter); then tf2onnx 1.17 (opset 13, NCHW input),
then `onnx.utils.extract_model` for the kept outputs. Every kept output is compared with
the TFLite model on random and real inputs; worst observed |ONNX - TFLite|: detector
0.0005 (outputs up to 212), landmarks 0.0018 px (up to 178), pose flag 8e-6, heatmap
0.0004. RKNN: FP16 (`do_quantization=False`), `target_platform=rk3588`, the mean/std
above baked in, so the NPU takes uint8 NHWC RGB.

OpenCV Zoo's `person_detection_mediapipe_2023mar.onnx` / `pose_estimation_mediapipe_2023mar.onnx`
were checked and not used: on the same input the landmark model differs from the
.task's by up to 38 px (different weights).

## What the port reproduces

From MediaPipe 0.10.35's C++ graph (`pose_landmarker_graph.cc`,
`pose_detector_graph.cc`, `pose_landmarks_detector_graph.cc` and their calculators):
letterboxed detector input with `warpPerspective` (bilinear, zero border); 2254 SSD
anchors (strides 8/16/32/32/32, fixed size); sigmoid scores (clipped at ±100) >= 0.5;
weighted NMS at IoU 0.5; letterbox removal; ROI centred on keypoint 0, size twice the
distance to keypoint 1, rotated so that keypoint 1 is up, x1.25 and square; association
of overlapping ROIs; 256x256 warp with replicated edges; pose flag > 0.5; sigmoid
visibility/presence; heatmap refinement (7x7, confidence >= 0.5, MediaPipe's
column/row weighting); projection back with the normalised-rect formula; tracking: the
next ROI comes from auxiliary landmarks 33/34, the detector runs only when there is no
ROI, a lost pose re-detects on the next frame; smoothing as VIDEO mode applies it:
visibility low-pass (alpha 0.1), then one-euro (min_cutoff 0.05, beta 80, derivative
cutoff 1) on pixel x/y/z scaled by the ROI size. Two MediaPipe behaviours kept on
purpose: the visibility filter is **not** reset when the pose is lost (its only input
gets no packet), so visibility ramps up again after re-acquisition; and the one-euro
filter restarts after a lost frame.

## Converting on the board

Needs the vendor kernel's `rknpu` driver, `librknnrt.so` in `/usr/lib` and
rknn-toolkit-lite2 in the app's `.venv` (see [ORANGE_PI_SETUP.md](../../ORANGE_PI_SETUP.md)).

```bash
features/pose/setup_converter.sh            # once; --rknn-version 2.5.0 to try the newer toolkit
.venv/bin/python features/pose/convert_rknn.py   # export-onnx, build-rknn, check
.venv/bin/python features/pose/benchmark.py
```

`setup_converter.sh` makes two private venvs under `.models/pose_npu/build/`
(TensorFlow 2.21 + tf2onnx for the ONNX export; rknn-toolkit2 + rknn-toolkit-lite2 for
the RKNN build and the check; their protobuf requirements conflict). The check runs
RKNN on the NPU and ONNX on ONNX Runtime on identical inputs: raw outputs, then the full
pipeline on MediaPipe's sample image (and `--video`/`--image` files of your own, which
stay on the board). It passes when raw landmark x/y differ by <= 3 px (256-px crop),
detection agrees on >= 98% of frames and landmark error p95 <= 0.01 / p99 <= 0.03
(normalised). The result, versions (TensorFlow, tf2onnx, onnx, rknn-toolkit2/lite2,
librknnrt) and every SHA-256 go into `manifest.json`. Re-running any step invalidates
the later ones.

Toolkit versions: 2.3.2 is the default; 2.5.0 (released 2026-10-13) needs
`setuptools<80.1` and has a report of slower runtime inference. Keep rknn-toolkit2,
rknn-toolkit-lite2 and `librknnrt.so` at the same version: a model built with a newer
toolkit may not load on an older runtime (the backend then falls back to CPU and
prints the init error).

## Data flows and dependencies

Pose inference stays on the device; nothing new is sent anywhere. The converter
downloads the .task (only if the runtime's copy is missing) from
storage.googleapis.com and MediaPipe's `pose.jpg` test image, both SHA-256-checked,
plus PyPI packages in `setup_converter.sh`. Runtime dependencies on the NPU path:
numpy, OpenCV (already required) and rknn-toolkit-lite2 + `librknnrt.so` (installed by
hand; not in requirements). No model weights are committed.

## Tests

`tests/test_pose_backend.py` (offline, no models): each reason the NPU is skipped,
forced/CPU/ONNX choices, init and mid-run failure fallback, the MediaPipe backend's
calls, RK3588 detection, `rknnlite` imported only in `engines.py` and never on import,
anchors (2254), letterbox, ROI rotation/size, decode + weighted NMS, association,
projection, heatmap refinement, smoothing (incl. visibility kept across a lost pose),
detect/track/lose flow, runtime wiring and the converter's setup message. A parity
test against real MediaPipe runs when converted ONNX models, onnxruntime, mediapipe,
the .task and the sample image are present, and is skipped otherwise.
`tests/test_toolkit_fall_detection.py` covers the `NPU (core pose)` status.

## Limits

Not yet run on an Orange Pi: RKNN conversion of these graphs, NPU accuracy and speed
are unverified until `convert_rknn.py` and `benchmark.py` run on the board. The port
matches MediaPipe within MediaPipe's own sensitivity to invisible input noise
(VALIDATION.md) but is not bit-identical. Single pose only (`num_poses=1`), as the
runtime uses.
