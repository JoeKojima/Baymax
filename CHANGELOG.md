# Update notes

## Unreleased — feature organization and toolkits (PR #11)

- Stack on PR #7 for all OS compatibility, setup, device and monitor infrastructure, including the Intel Mac profile. The PR review diff excludes that inherited foundation.
- Scope location/weather and opt-in public information/headline/feed toolkits to this branch; optional CPU fall detection is now inherited from PR #7. Existing camera frames, bounded provider calls, explicit preferences, monitoring, controls and disabled behavior are preserved.
- Organize features under sibling `memory_palace/`, `person_recognition/` and `toolkits/` namespaces; memory/person folders reserve integration boundaries. Prompt/identity/recall changes and Memory Palace implementation belong to PR #13, not this PR.
- Runtime modifications here are limited to feature import paths, toolkit dispatch/lifecycle and monitoring. Original v8 prompt/models/audio behavior remain covered by strict upstream parity checks; v9 matches main.
- Previous validation: 169 combined offline checks passed on Windows/Python 3.12 (99 core, 70 toolkit). Updated validation on 2026-10-21 passes all 171 checks: 101 core and 70 toolkit, zero failures/errors/skips. Actual Gemini/hardware, Mac/Linux/Orange Pi installs, fall accuracy/performance, Docker and live integration remain pending. Earlier provider smoke checks do not establish current provider availability or full conversation correctness.

## Unreleased — cross-platform setup scope (PR #7)

- Added experimental Intel macOS/Python 3.12 requirements selection with compatible MediaPipe/NumPy/OpenCV/spaCy/JAX/Numba pins. Intel wheel-only dependency resolution succeeds; native installation and the student's 2020 Intel Mac/macOS 15.7.9 device testing remain pending.
- Retained Windows/macOS/Linux clickables, private Python/dependency installation, key validation, local settings, native device identity, live transcript/stop monitor, ARM dependency selection and the explicit Debian prerequisite step.
- Optional CPU fall detection is now included in PR #7: Setup enable/disable setting with a safe default, shared camera frames, checksum-verified pose model, local status/FPS and possible-fall Gemini check-ins. Location/weather and other information tools remain in PR #11.
- Added an HTTP regression for fresh configuration, enabling, reopening and disabling fall detection; the release gate now requires its dedicated toolkit tests. Native Mac/Linux/ARM performance and accuracy checks remain pending.
- Brought the branch up to merged main. No production deployment changes are introduced relative to main. Base v8 prompt, models and audio processing remain unchanged apart from documented OS/monitor hooks.
- Validation on 2026-10-21: all 111 offline checks pass on Windows/Python 3.12 (94 core, 17 fall toolkit), with zero failures/errors/skips. Real MediaPipe CPU initialization/inference/cleanup also passes with a synthetic blank image and a checksum-verified cached model; no camera/microphone or cloud requests. Dependency resolution succeeds for Windows x86_64, Apple Silicon macOS, Intel macOS, Linux x86_64 and Linux ARM64 profiles (98/101/109/95/103 packages respectively). Resolution does not establish native installation, ARM source-build success, fall accuracy or performance. Mac/Linux hardware, voice analysis, Gemini access and end-to-end stop checks remain pending. Lead approval is still required before merge/deployment.
