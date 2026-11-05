# Experimental CPU fall-detection toolkit

Enable **experimental fall detection (CPU)** in devUI, select a camera, Save, then restart Ember. It is off by default. `BAYMAX_FALL_DETECTION_ENABLED=1` is the equivalent private .env setting; `0` disables it. A disabled toolkit never loads its pose model or starts its inference worker. This is an optional shared feature, separate from `BAYMAX_TOOLKIT_ENABLED`, which controls location/weather.

## Inputs, outputs and processing

The existing camera thread shares copies of its frames with one CPU inference worker. The toolkit never opens a second camera. A single pending frame replaces older queued frames; frames over 0.5 seconds old are discarded. Camera capture and Gemini audio/video processing do not wait for inference. CPU contention can still affect conversation performance, so measure it on the target board.

Google's MediaPipe full PoseLandmarker float16 model (version 1) runs with an explicit CPU delegate. The first enabled run downloads a ~9.4 MB model from storage.googleapis.com into ignored `.models/`, verifies its recorded SHA-256, and caches it. A corrupt model makes detection unavailable; remove that cache file and retry. No NPU/GPU driver is required for this backend.

Pose landmarks feed the repository's existing FallDetector, based on JoeKojima/Baymax commit `6d2386101d676df476eb97554963d60f1b5950b2`. `fall_detection/UPSTREAM.json` records both the upstream and locally patched hashes. The local patch accepts an optional monotonic capture timestamp so asynchronous inference does not distort motion timing. Callers without a timestamp retain existing behavior. Thresholds, upright-history, one-second warmup and two-second stay-down checks are unchanged.

## Built-in v10 parallel processing

The OPI4 v10 runtime uses its configured lite/full/heavy model and built-in fall
settings. `LatestVision` runs model loading and pipe I/O on a worker thread;
`Remote` runs MediaPipe in a child process. Capture, Gemini video delivery and
camera observers continue while inference runs. There is one in-flight frame,
one replaceable pending frame and one completed result, rather than a growing
queue. Submitted OpenCV frames are read-only until inference finishes.

Completed samples carry their capture times. Each result advances fall/face
tracking once; samples older than one second are discarded. The stream draws
landmarks on the corresponding source frame, and clears stale overlays. Pose
failure leaves camera delivery and conversation running, with an unavailable
message in the monitor and stream. Child-model startup is bounded to 60 seconds
after the existing model download; that download also runs off the camera thread.
individual child-process inference calls time out after five seconds. Shutdown
cancels an initialized child if inference is stuck. Initialization still in
progress exits when loading completes or its startup timeout expires.

When built-in v10 detection is enabled, the optional toolkit reuses its existing
check-in path instead of loading a second pose model. Camera observers and
session tasks remain active. With built-in detection disabled, the optional
toolkit retains its independent opt-in behavior.

These changes remove inference waits from capture; they do not establish adequate
pose throughput on the OPI4. The detector still resets tracking across torso gaps
over 0.5 seconds. Target-board inference timing and concurrent conversation,
camera/eyes, safe positive/negative clips and false-positive checks remain required.
Tests: `test_toolkit_parallel_vision.py`, `test_toolkit_vision_process.py` and
`test_toolkit_fall_detection.py`. Processing adds no provider or device stream;
the existing v10 Gemini and configured fall-event data flows still apply.

The monitor displays disabled/loading/running/unavailable/stopped state, measured inference time/FPS and compact possible-fall events. Backend failure leaves conversation available and reports detection unavailable. The configured camera must be available; no camera means no detection.

A confirmed algorithm event creates an uncertain **possible-fall check-in** in the active Gemini conversation: ask whether the person is okay. Stale events over 15 seconds old are discarded. A failed Gemini check-in is recorded locally and does not crash the conversation. No caregiver email, SMS, emergency call or production webapp alert is enabled. There is no independent alarm if Gemini is disconnected; the local event remains visible in the monitor.

## Dependencies and ARM compatibility

- Windows/macOS desktop pins remain in requirements-pc.txt (MediaPipe 0.10.35).
- Linux ARM64 uses requirements-orange-pi.txt automatically. MediaPipe 0.10.18 has an official Python 3.12 ARM64 wheel, but requires NumPy <2. That profile pins NumPy 1.26.4, OpenCV 4.10, spaCy 3.7.5 and compatible numerical dependencies separately. Core prompts/models/audio rates are unchanged; numerical/NLP library versions differ and need on-board verification.
- Praat-Parselmouth 0.4.7 has no ARM wheel and must build from source. The Debian prerequisite clickable installs build tools; source-build success is still unverified on the Orange Pi.

`BAYMAX_FALL_FPS` may set a target from 2 through 30 (default: existing core target 15). Actual FPS depends on inference time; a target is not a measured throughput guarantee. Tracking gaps above the upstream detector's 0.5-second reset threshold and low frame rates can materially affect detection. Profile CPU, memory, thermals, audio latency and missed frames before selecting deployment settings.

## Data flows

Pose inference, landmarks and angle/motion calculations remain local. The monitor receives state, timing and a small angle/result summary, not camera frames or joint coordinates. A possible-fall text check-in is sent to Gemini over the existing session. The existing runtime already sends camera/audio data to Gemini and records audio locally; enabling this feature does not remove that behavior. The model download sends no key or private camera data.

## Validation protocol

Run `python check_release.py` to include synthetic detector logic, worker failure/backlog behavior, model checksum, settings, passive monitor and Gemini check-in tests. The real CPU backend was smoke-tested on Windows with a synthetic blank image; that is not evidence of fall accuracy or ARM performance.

On the actual Orange Pi 5 Max, record the OS/kernel/architecture, installation/source-build result, camera position and actual inference FPS. Use consented prerecorded test clips or controlled safe scenarios to check ordinary sitting/lying/bending, occlusion, camera loss, possible falls, warmup/stay-down/cooldown, failure recovery and simultaneous Gemini conversation. Compare false positives, misses and check-in delays. Do not ask a person to perform an unsafe fall. This development feature is not validated as an emergency-monitoring system. Lead approval requires real-device evidence.

## Optional future NPU backend

Orange Pi 5 Max's Rockchip NPU requires a compatible RKNN model conversion/runtime and matching board driver. The CPU MediaPipe `.task` model is not automatically accelerated by installing an NPU package. Treat RKNN support as a separate backend with its own output/accuracy, performance and driver validation; it is not implemented here.

References: [Google pose task](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker/python), [ARM MediaPipe wheel](https://pypi.org/project/mediapipe/0.10.18/#files), [Rockchip RKNN stack](https://github.com/airockchip/rknn-toolkit2).
