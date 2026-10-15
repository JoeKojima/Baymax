# Update notes

## Unreleased - first-run clickable installation

- Clarified first-use and daily click order: Setup once, Monitor, Start Ember, then Stop Ember; Monitor can open before or after Start.

- OS clickables now bootstrap private Python 3.12, full requirements and the spaCy model without terminal commands on supported configurations.
- Installation readiness requires offline tests; requirement changes retrigger installation, failures remain retryable, and concurrent environment setup is blocked.
- Setup creates Desktop/clickables shortcuts pointing to the extracted repository. Start opens Setup if the key is missing.
- Nine installer regression tests bring the offline total to 80 (70 core/development, 10 toolkit).
- A fresh project environment was installed and tested on this Windows PC. Mac/Linux fresh installs and physical hardware are unverified; Intel/Rosetta Macs remain unsupported.

## Earlier - OS launchers and Gemini startup verification

- Grouped launchers into devops/clickables/Windows, macOS and Linux, sharing launch.py.
- Start Ember now verifies key acceptance and configured model visibility with Google before importing the conversation runtime or opening hardware.
- Failures explain invalid/denied access, unavailable models, quota, service or network problems without echoing keys/provider exceptions. Metadata success does not guarantee Live-session quota or generation.
- Added nine offline regression tests for the preflight and startup gate; no real keys or providers are used by these tests. All 71 offline checks pass locally on Windows (61 core/development, 10 toolkit); all 12 launcher relative paths were checked.

## Earlier - macOS device compatibility

- Added native Mac camera names and persistent camera IDs matching OpenCV's AVFoundation ordering.
- Save/test resolve the same camera after reordering; missing or ambiguous cameras require reselection.
- Discovery reads Mac camera metadata without starting capture; permission errors explain System Settings.
- Added 14 Mac contract simulations and three HTTP integration simulations; generic tests no longer depend on the host OS.
- Added a ready-to-enable Windows/macOS GitHub offline workflow example and a real-Mac verification checklist. Hosted testing is pending because this CLI login lacks workflow authorization.
- Local Windows result: 62 offline tests pass (52 core/development and 10 toolkit). Hosted macOS results must be checked in the PR; physical Mac and live Gemini validation remain pending.

## Earlier - Ember desktop development tools

### Added
- Local Gemini key/device setup UI with microphone, speaker and camera tests.
- Conversation monitor with incremental transcripts, grouped toolkit events and reply timing.
- Desktop hardware adapters and Windows/macOS/Linux launchers.
- Location/weather toolkit with approximate location, city clarification and caching.
- Setup documentation and offline tests for these additions plus upstream runtime parity checks.
- `check_release.py`: separate core/toolkit offline results and a pending live-review checklist.
- Testing protocol and PR template requiring core, toolkit and integration evidence.

### Verification and limitations
- 45 offline checks pass on Windows: 35 core/development checks and 10 toolkit checks. The total includes three tests for the review CLI itself.
- A Windows Gemini conversation and location/weather events were observed locally.
- Fresh-machine installation, macOS/Linux hardware, deployed robot version/parity and the full live checklist remain pending.
- No production deployment or merge is implied by these checks.
