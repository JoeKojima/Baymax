# Experimental CPU fall-detection toolkit

Enable **experimental fall detection (CPU)** in devUI, select a camera, Save, then restart Ember. It is off by default. The demo screen and synthetic replay (below) need no setting. `BAYMAX_FALL_DETECTION_ENABLED=1` is the equivalent private .env setting; `0` disables it. A disabled toolkit never loads its pose model or starts its inference worker. This is an optional shared feature, separate from `BAYMAX_TOOLKIT_ENABLED`, which controls location/weather.

## Inputs, outputs and processing

The existing camera thread shares copies of its frames with one CPU inference worker. The toolkit never opens a second camera. A single pending frame replaces older queued frames; frames over 0.5 seconds old are discarded. Camera capture and Gemini audio/video processing do not wait for inference. CPU contention can still affect conversation performance, so measure it on the target board.

Google's MediaPipe full PoseLandmarker float16 model (version 1) runs with an explicit CPU delegate. The first enabled run downloads a ~9.4 MB model from storage.googleapis.com into ignored `.models/`, verifies its recorded SHA-256, and caches it. A corrupt model makes detection unavailable; remove that cache file and retry. No NPU/GPU driver is required for this backend.

Pose landmarks feed the repository's existing FallDetector, copied unchanged from JoeKojima/Baymax commit `6d2386101d676df476eb97554963d60f1b5950b2` with its hash in `fall_detection/UPSTREAM.json`. Core threshold values (45-degree trunk angle, 25-degree/s angular velocity, 0.12 normalized hip descent/s, two confirming frames, eight-frame history and three-second cooldown) are passed into it. Its existing upright-history, one-second warmup and two-second stay-down checks remain unchanged.

The monitor displays disabled/loading/running/unavailable/stopped state, measured inference time/FPS and compact possible-fall events. Backend failure leaves conversation available and reports detection unavailable. The configured camera must be available; no camera means no detection.

A confirmed algorithm event creates an uncertain **possible-fall check-in** in the active Gemini conversation: ask whether the person is okay. Stale events over 15 seconds old are discarded. A failed Gemini check-in is recorded locally and does not crash the conversation. No caregiver email, SMS, emergency call or production webapp alert is enabled. There is no independent alarm if Gemini is disconnected; the local event remains visible in the monitor.

## Decision logic (pure, testable)

`fall_logic.py` holds the decision without MediaPipe or a camera: `FallDecision(**settings).update(landmarks, now)` takes one frame's 33 normalized landmarks (or None) and a timestamp, and returns the upstream result (`fall_detected`, `fall_pending`, `fall_active`, `trunk_angle`, `angular_vel`, `hip_descent_vel`) plus `posture` and `countdown_s`. It wraps the unchanged upstream `FallDetector` (subclass `ClockedFallDetector` only lets the caller supply the time); a test replays every synthetic sequence through both and requires identical decisions frame by frame. `DEFAULT_SETTINGS` equals the v8 `FALL_*` constants (also tested). The worker now uses `FallDecision`, so live decisions are unchanged.

In words: a frame is *suspicious* when the shoulder-hip line is more than 45 degrees from vertical **and** it is rotating faster than 25 degrees/s or the hips drop faster than 0.12 frame-heights/s (over the last 8 frames), the person was under 30 degrees within the last 1.5 s, and tracking has been stable for 1 s. Two suspicious frames open a *pending* fall; it is cancelled if the trunk returns under 30 degrees and confirmed after 2 s otherwise (hidden-torso frames do not cancel it). A 3 s cooldown follows. Posture labels (standing / sitting / upright / bending / lying / going down / on floor) are display-only and never change the decision.

## Synthetic ("made-up") pose sequences

`fall_poses.py` generates idealized stick-figure sequences in MediaPipe's 33-landmark layout with timestamps: `fall_poses.scenarios()` or `fall_poses.scenario("fast_fall", fps=15)`; `body()`, `keyframes()` and `generate()` build new ones. No images or recordings of anyone. The robot rig or an evaluation harness can import it directly. Results with the v8 thresholds at 15 FPS (`tests/test_toolkit_fall_poses.py`):

| Sequence | Ideal | Current logic |
|---|---|---|
| standing still, walking, normal sit, slow (6 s) lie-down in bed, already in bed, bend-and-pick-up, flickering torso, hips behind desk, leaving frame | no fall | no fall (bend opens then cancels a pending fall) |
| fast fall then lying still; with legs hidden; torso hidden 0.3 s mid-fall; ending below the frame; fall then getting up | one fall | one fall, confirmed 2.67 s after onset (also at 10/8/5 FPS, within 2-3 s) |
| brisk (3 s) lie-down in bed | no fall | **false alarm** (peak rotation exceeds 25 deg/s, then stays down) |
| bent over for 2.5 s (tying a shoe) | no fall | **false alarm** (stay-down only checks the trunk angle) |
| fall that leaves the bottom of the frame at once | fall | **missed** (one suspicious frame visible; two required) |
| fall toward the camera (along its axis) | fall | **missed** (2D trunk line stays near vertical) |

The four bold rows are `expectedFailure` tests: known limitations needing a threshold/design decision and real-camera evidence, not silent fixes. Passing synthetic tests shows the rules behave as designed on clean input; it says nothing about MediaPipe accuracy, camera placement or real falls.

## Live demo mode

For showing the feature in a room: run Ember, open devUI, follow **Fall demo** (`http://127.0.0.1:8766/fall-demo`).

- **Demo mode** sends possible-fall alerts to that screen (banner, countdown, the list of what would fire). **Ember speaks the check-in** decides whether the usual Gemini check-in is still sent; unticked, Ember stays quiet and the monitor records that. No caregiver email, SMS, call or webapp alert exists in this build, in demo mode or otherwise; the screen says so.
- **Replay synthetic fall** (or bend / sit / lie in bed) pushes a made-up sequence, in real time, through a fresh `FallDecision` with the worker's thresholds and the *same* alert path as camera falls (monitor event, eyes, check-in). It works with fall detection disabled and with no camera. If Ember is not running, devUI computes the replay itself and animates it; nobody is alerted and Ember does not speak.
- With fall detection enabled (Device setup, restart), the screen shows the live pose skeleton, posture, trunk angle and countdown from camera frames. **Show camera preview** displays the runtime's existing MJPEG stream (port 8080) under the skeleton; no camera or microphone is opened by the toolkit, devUI or the page.
- The switch reaches the runtime through the gitignored `.runtime-fall-demo.json`, bound to the running Ember's PID and polled by the monitor thread; a restarted Ember starts with demo mode off. It changes no `.env` setting, no systemd unit and no production alert.

## Dependencies and ARM compatibility

- Windows/macOS desktop pins remain in requirements-pc.txt (MediaPipe 0.10.35).
- Linux ARM64 uses requirements-orange-pi.txt automatically. MediaPipe 0.10.18 has an official Python 3.12 ARM64 wheel, but requires NumPy <2. That profile pins NumPy 1.26.4, OpenCV 4.10, spaCy 3.7.5 and compatible numerical dependencies separately. Core prompts/models/audio rates are unchanged; numerical/NLP library versions differ and need on-board verification.
- Praat-Parselmouth 0.4.7 has no ARM wheel and must build from source. The Debian prerequisite clickable installs build tools; source-build success is still unverified on the Orange Pi.

`BAYMAX_FALL_FPS` may set a target from 2 through 30 (default: existing core target 15). Actual FPS depends on inference time; a target is not a measured throughput guarantee. Tracking gaps above the upstream detector's 0.5-second reset threshold and low frame rates can materially affect detection. Profile CPU, memory, thermals, audio latency and missed frames before selecting deployment settings.

## Data flows

Pose inference, landmarks and angle/motion calculations remain local. The monitor receives state, timing and a small angle/result summary, not camera frames or joint coordinates. **Only while demo mode is on**, the local status file also holds the latest 33 normalized joint points, posture and countdown for the demo screen (local file, ~5 updates/s, not sent anywhere). The camera preview is the runtime's existing local MJPEG stream; devUI does not proxy or store it. Synthetic replays contain no personal data. A replayed fall sends the same check-in text to Gemini as a camera fall unless "Ember speaks" is off. A possible-fall text check-in is sent to Gemini over the existing session. The existing runtime already sends camera/audio data to Gemini and records audio locally; enabling this feature does not remove that behavior. The model download sends no key or private camera data.

## Validation protocol

Run `python check_release.py` to include the made-up pose sequences (`tests/test_toolkit_fall_poses.py`; MediaPipe not needed), demo mode/replay/dev-UI routes (`tests/test_toolkit_fall_demo.py`), synthetic detector logic, worker failure/backlog behavior, model checksum, settings, passive monitor and Gemini check-in tests. The real CPU backend was smoke-tested on Windows with a synthetic blank image; that is not evidence of fall accuracy or ARM performance.

On the actual Orange Pi 5 Max, record the OS/kernel/architecture, installation/source-build result, camera position and actual inference FPS. Use consented prerecorded test clips or controlled safe scenarios to check ordinary sitting/lying/bending, occlusion, camera loss, possible falls, warmup/stay-down/cooldown, failure recovery and simultaneous Gemini conversation. Compare false positives, misses and check-in delays. Do not ask a person to perform an unsafe fall. This development feature is not validated as an emergency-monitoring system. Lead approval requires real-device evidence.

## Optional future NPU backend

Orange Pi 5 Max's Rockchip NPU requires a compatible RKNN model conversion/runtime and matching board driver. The CPU MediaPipe `.task` model is not automatically accelerated by installing an NPU package. Treat RKNN support as a separate backend with its own output/accuracy, performance and driver validation; it is not implemented here.

References: [Google pose task](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker/python), [ARM MediaPipe wheel](https://pypi.org/project/mediapipe/0.10.18/#files), [Rockchip RKNN stack](https://github.com/airockchip/rknn-toolkit2).
