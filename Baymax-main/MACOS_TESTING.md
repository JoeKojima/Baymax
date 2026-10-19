# macOS support and verification

## What has been simulated

`python -m unittest discover -s tests -p test_macos_devices.py` runs Mac contract tests on any OS with the test dependencies installed. It simulates native camera names/IDs, OpenCV index ordering, hot-plug reorder/rename, duplicate names, disconnected cameras, disabled cameras, CoreAudio listing/selection and camera permission states. HTTP tests also simulate preserving camera identity when saving/testing. No real camera/microphone or cloud service is used.

The implementation enumerates AVFoundation video plus muxed devices and sorts unique IDs in the order used by the pinned OpenCV 4.13 backend. Native names appear in devUI; the saved UID identifies the camera after index/name changes. Enumeration reads metadata only; Test camera opens capture in a subprocess. macOS-only PyObjC dependencies are conditional in requirements-pc.txt. Microphone/speaker selection uses PortAudio/CoreAudio names; indistinguishable duplicate audio names still need manual selection.

Simulation is not a macOS VM and does not validate Apple's permission prompts, drivers, sample-rate compatibility or live Gemini behavior. No actual Mac hardware has been tested by the Windows simulation.

## Hosted macOS checks

The ready-to-enable example `docs/ci/desktop-offline.yml.example` installs desktop dependencies and runs the offline gate on GitHub's Windows/macOS runners. On macOS it also checks native AVFoundation/OpenCV/sounddevice imports. No devices, credentials or live providers are used. Hosted testing has NOT run: the current CLI authorization lacks the workflow scope, so GitHub rejected the workflow upload. A maintainer can copy the example to `.github/workflows/desktop-offline.yml` using an authorized login, then check the PR Checks tab for actual results. This does not install the spaCy language model or certify a complete runtime launch, Intel/Apple Silicon coverage or physical-device behavior.

## Real Mac checklist (pending until tested)

On native Apple Silicon, try `devops/clickables/macOS/1 - Setup.command`: it installs private Python 3.12, the requirements and spaCy model automatically. Intel/Rosetta Macs are blocked by the pinned MediaPipe dependency. Manual setup remains documented in PC_SETUP.md; never use the Windows lock file on Mac.

- [ ] Record Mac model, architecture, macOS version, Python version and commit.
- [ ] Install dependencies and the spaCy model in a fresh environment.
- [ ] Run `python check_release.py --report .local-review/report.md`.
- [ ] Run `python devUI.py`; verify native camera and CoreAudio names.
- [ ] Test microphone, speaker and camera deliberately. In System Settings > Privacy & Security, allow Microphone/Camera for the launching app (often Terminal). Denied permission must show a useful error.
- [ ] When first testing the camera, approve the macOS prompt, then click Test camera again if needed. Discovery itself never starts capture.
- [ ] Save, reopen, unplug/replug devices and refresh; verify the selected camera UID resolves correctly or reports missing. Do not assume two identical audio names distinguish physical units.
- [ ] Enter your own Gemini key locally. Start Ember with `python run_pc.py`; complete core conversation, interruptions, monitor and stop/restart checks in TESTING_PROTOCOL.md.
- [ ] Complete location/weather integration checks and record provider failures/recovery.
- [ ] Test `.command` launchers in Finder; permissions or downloaded-file security settings may require manual handling.

Gemini runtime capture sends audio/video to Google and stores session audio locally. Start it only deliberately. Keep credentials and private recordings out of PR evidence. Mark untested items pending, not pass.

## References

- [OpenCV 4.13 AVFoundation backend: camera index ordering and permission handling](https://github.com/opencv/opencv/blob/4.13.0/modules/videoio/src/cap_avfoundation_mac.mm)
- [Apple camera display names](https://developer.apple.com/documentation/avfoundation/avcapturedevice/localizedname)
- [PyObjC AVFoundation dependency](https://pypi.org/project/pyobjc-framework-AVFoundation/11.1/)
