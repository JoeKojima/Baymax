# Experimental CPU fall-detection toolkit

Enable **experimental fall detection (CPU)** in devUI, select a camera, Save, then restart Ember. It is off by default. `BAYMAX_FALL_DETECTION_ENABLED=1` is the equivalent private .env setting; `0` disables it. A disabled toolkit never loads its pose model or starts its inference worker. This is an optional shared feature, separate from `BAYMAX_TOOLKIT_ENABLED`, which controls location/weather.

## Inputs, outputs and processing

The existing camera thread shares copies of its frames with one CPU inference worker. The toolkit never opens a second camera. A single pending frame replaces older queued frames; frames over 0.5 seconds old are discarded. Camera capture and Gemini audio/video processing do not wait for inference. CPU contention can still affect conversation performance, so measure it on the target board.

Google's MediaPipe full PoseLandmarker float16 model (version 1) runs with an explicit CPU delegate. The first enabled run downloads a ~9.4 MB model from storage.googleapis.com into ignored `.models/`, verifies its recorded SHA-256, and caches it. A corrupt model makes detection unavailable; remove that cache file and retry. No NPU/GPU driver is required for this backend.

Pose landmarks feed the repository's existing FallDetector, copied unchanged from JoeKojima/Baymax commit `6d2386101d676df476eb97554963d60f1b5950b2` with its hash in `fall_detection/UPSTREAM.json`. Core threshold values (45-degree trunk angle, 25-degree/s angular velocity, 0.12 normalized hip descent/s, two confirming frames, eight-frame history and three-second cooldown) are passed into it. Its existing upright-history, one-second warmup and two-second stay-down checks remain unchanged.

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

## NPU backend (core pose)

When the toolkit reuses the core's pose (`shared_pose`, v10), that pose can run on the
Orange Pi 5 Max's RK3588 NPU: see [features/pose](../pose/README.md). The status then
reads **Running on NPU (core pose)** and the inference time is the NPU pipeline's. It is
used only after the models are converted and pass an accuracy check on the board, and
falls back to MediaPipe on the CPU otherwise. The toolkit's own non-shared `CpuPose`
backend is unchanged.

References: [Google pose task](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker/python), [ARM MediaPipe wheel](https://pypi.org/project/mediapipe/0.10.18/#files), [Rockchip RKNN stack](https://github.com/airockchip/rknn-toolkit2).
