# Update notes

## Unreleased — cross-platform setup scope (PR #7)

- Added experimental Intel macOS/Python 3.12 requirements selection with compatible MediaPipe/NumPy/OpenCV/spaCy/JAX/Numba pins. Intel wheel-only dependency resolution succeeds; native installation and the student's 2020 Intel Mac/macOS 15.7.9 device testing remain pending.
- Retained Windows/macOS/Linux clickables, private Python/dependency installation, key validation, local settings, native device identity, live transcript/stop monitor, ARM dependency selection and the explicit Debian prerequisite step.
- Moved location/weather and optional CPU fall toolkit implementation, controls and toolkit tests to PR #11. This branch has core compatibility tests only; no toolkit/provider behavior is claimed as tested here.
- Brought the branch up to merged main. No production deployment changes are introduced relative to main. Base v8 prompt, models and audio processing remain unchanged apart from documented OS/monitor hooks.
- Offline validation and real-device limitations are recorded in the PR. Windows simulations do not certify Mac/Linux hardware, ARM source builds, voice analysis, Gemini access or end-to-end stop behavior. Lead approval is still required before merge/deployment.
