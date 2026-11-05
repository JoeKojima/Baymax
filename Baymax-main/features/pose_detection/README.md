# Pose Detection

`vision_process.py` owns model subprocesses and the latest-frame inference worker.
`worker.py` owns camera observers, shared session tasks and the optional CPU
detector. `fall_detection/` retains the vendored detector and its provenance.
Inputs are existing camera frames, never another capture stream. Outputs are
local landmarks and possible-fall events, routed through the existing v10
check-in and configured event path. See `../toolkits/FALL_DETECTION.md` for data
flows, dependencies, enablement, timeouts, tests and pending OPI4 measurements.
`EMBER_POSE_TRACKING` and `EMBER_BUILTIN_FALL_DETECTION` override the device TOML
switches; saved explicit opt-outs are retained when these variables are absent.
