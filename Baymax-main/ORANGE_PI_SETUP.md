# Orange Pi 5 Max: Debian 13, XFCE/Xorg (experimental)

This is a native Linux ARM64 CPU profile. Dependency resolution and simulated tests were checked on the development PC; actual Orange Pi installation, device behavior, source compilation, fall accuracy and throughput remain unverified.

1. Extract the complete current branch ZIP into a permanent folder on the Pi.
2. Open devops/clickables/Linux. On a minimal Debian 13 system, run **0 - Debian Prerequisites** first. It lists system libraries/build tools, asks permission, and uses sudo/pkexec for apt installation. It does not install NPU drivers, replace the kernel or configure an audio server. Ensure XFCE desktop audio and a browser already work.
3. Open **1 - Setup**. It installs private Python 3.12 and automatically selects requirements-orange-pi.txt for Linux aarch64/arm64. Expect a potentially long native build for Praat-Parselmouth. A resolver pass does not prove that compilation will succeed.
4. Enter your own Gemini key, select/test mic, speaker and camera, and optionally enable **experimental fall detection (CPU)**. Save. It is off by default and requires a camera.
5. Open **2 - Monitor**, then **3 - Start Ember**. The first enabled fall run downloads/verifies the pose model. Look for **Running on CPU**, actual FPS and inference milliseconds; disabled/loading/unavailable is not a working detector.
6. Use **4 - Stop Ember**. Test device release and restart; keep your evidence for review.

If XFCE opens `.sh` files in an editor, use its file permissions/execution options to run them in a terminal; executing scripts depends on Thunar settings. The current launcher is not a packaged XFCE desktop application. Localized Desktop-folder layouts, USB/CSI camera drivers, user device permissions and sound-server routing can require OS-specific setup.

The prerequisite script is only for Debian 13. It installs curl/certificates, PortAudio, GL/EGL/GLib and sound-file libraries, ALSA plugins, compiler/CMake/pkg-config tools, xdg-utils and XFCE Terminal. It also offers Firefox ESR in the displayed package list when no common browser command is found. This system-package step is separate from the private Python environment and requires administrator authorization. NPU support is not installed. A 64-bit OS and native ARM64 Python are required; 32-bit armhf is not supported by this profile.

See [fall toolkit](features/toolkits/FALL_DETECTION.md) for model/algorithm provenance, synthetic checks, known limits and the real-device test protocol. Full conversation/fall integration must be measured on the board before approval or deployment.
