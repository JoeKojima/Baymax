# Orange Pi 5 Max: Debian 13, XFCE/Xorg (experimental)

This is a native Linux ARM64 CPU profile (optional NPU pose: see below). Dependency resolution and simulated tests were checked on the development PC; actual Orange Pi installation, device behavior, source compilation, fall accuracy and throughput remain unverified.

1. Extract the complete current branch ZIP into a permanent folder on the Pi.
2. Open devops/clickables/Linux. On a minimal Debian 13 system, run **0 - Debian Prerequisites** first. It lists system libraries/build tools, asks permission, and uses sudo/pkexec for apt installation. It does not install NPU drivers, replace the kernel or configure an audio server. Ensure XFCE desktop audio and a browser already work.
3. Open **1 - Setup**. It installs private Python 3.12 and automatically selects requirements-orange-pi.txt for Linux aarch64/arm64. Expect a potentially long native build for Praat-Parselmouth. A resolver pass does not prove that compilation will succeed.
4. Enter your own Gemini key, select/test mic, speaker and camera, and optionally enable **experimental fall detection (CPU)**. Save. It is off by default and requires a camera.
5. Open **2 - Monitor**, then **3 - Start Ember**. The first enabled fall run downloads/verifies the pose model. Look for **Running on CPU**, actual FPS and inference milliseconds; disabled/loading/unavailable is not a working detector.
6. Use **4 - Stop Ember**. Test device release and restart; keep your evidence for review.

If XFCE opens `.sh` files in an editor, use its file permissions/execution options to run them in a terminal; executing scripts depends on Thunar settings. The current launcher is not a packaged XFCE desktop application. Localized Desktop-folder layouts, USB/CSI camera drivers, user device permissions and sound-server routing can require OS-specific setup.

The prerequisite script is only for Debian 13. It installs curl/certificates, PortAudio, GL/EGL/GLib and sound-file libraries, ALSA plugins, compiler/CMake/pkg-config tools, xdg-utils and XFCE Terminal. It also offers Firefox ESR in the displayed package list when no common browser command is found. This system-package step is separate from the private Python environment and requires administrator authorization. NPU support is not installed by it (see Optional: pose on the NPU). A 64-bit OS and native ARM64 Python are required; 32-bit armhf is not supported by this profile.

## Optional: pose on the NPU

Ember runs MediaPipe Pose on the CPU unless the RK3588's NPU is ready; then the camera
thread's pose (fall detection, face presence, the live view and the fall toolkit) uses
the NPU with the same model weights ([features/pose](features/pose/README.md)). Nothing
changes until all of the steps below are done, and any failure falls back to the CPU.
These steps have not yet been run on an Orange Pi 5 Max; record what you see.

1. **Check the kernel and NPU driver.** RKNN needs Rockchip's vendor kernel (6.1 series)
   with the `rknpu` driver (0.9.8 is current); a mainline kernel's `rocket` driver does
   not work with RKNN.

   ```bash
   uname -r                                          # vendor kernels look like 6.1.x-...rk35xx / -rockchip-rk3588
   ls /sys/bus/platform/drivers | grep -i -e rknpu -e rocket   # need: RKNPU
   sudo cat /sys/kernel/debug/rknpu/version          # e.g. RKNPU driver: v0.9.8
   tr '\0' '\n' < /proc/device-tree/compatible       # need: rockchip,rk3588
   ```

   No `RKNPU` means this image cannot use the NPU: keep the CPU (nothing to do) or move
   to an image with the vendor kernel. Changing the kernel is outside Ember's setup.
2. **Install the NPU runtime library (administrator step).** Use the same version as
   step 3 (2.3.2 here; 2.5.0 SHA-256 `4b6cc575bedf8952b13db4b1190fa3b3d2ac213f00be774ff6ccd19d651970c0`):

   ```bash
   curl -fLO https://github.com/airockchip/rknn-toolkit2/raw/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so
   echo "d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8  librknnrt.so" | sha256sum -c
   sudo install -m 0644 librknnrt.so /usr/lib/librknnrt.so && sudo ldconfig
   ```
3. **Install rknn-toolkit-lite2 into Ember's environment** (Python 3.12 aarch64 wheel;
   not in requirements, so updates never add it on their own):

   ```bash
   cd Baymax-main
   .venv/bin/python -m pip install rknn-toolkit-lite2==2.3.2
   ```

   Keep it at the version the converter uses (step 4): Ember only uses the NPU on its
   own when the accuracy check ran with the same rknn-toolkit-lite2 version.
4. **Convert the models on the board.** First stop Ember with **4 - Stop Ember**: the
   check uses NPU core 0 like the running robot. About 3 GB of downloads/disk for the
   converter's own environments under `.models/pose_npu/build/`; takes a while:

   ```bash
   features/pose/setup_converter.sh               # add --rknn-version 2.5.0 to try the newer toolkit
   .venv/bin/python features/pose/convert_rknn.py
   ```

   It ends with `Accuracy check PASSED` (or `FAILED (<what missed its limit>)`: Ember
   then stays on the CPU) and writes `.models/pose_npu/manifest.json`. The check
   compares the NPU with ONNX Runtime (landmark positions and which landmarks are
   visible) on still frames and on a 72-frame tracked sequence (including a fall); add `--video some_clip.mp4` to also check on your own footage
   (it stays on the board). To repeat only the check: `.models/pose_npu/build/venv-rknn/bin/python
   features/pose/convert_rknn.py check`.
5. **Measure** (Ember still stopped, so it does not compete for the CPU and NPU):
   `.venv/bin/python features/pose/benchmark.py` prints milliseconds per frame for
   MediaPipe on the CPU and for the NPU, and how close their landmarks are.
6. **Confirm it is active.** Start Ember. The log shows
   `[POSE] Pose backend: NPU (RK3588) on NPU core 0 (...)`; otherwise
   `[POSE] Pose backend: CPU (MediaPipe). NPU skipped: <reason>`; if `librknnrt` or the
   driver changed since the check (e.g. after a kernel update) the reason says so: re-run
   the check. A `[POSE] Warning:` line after the NPU line means the downloaded MediaPipe
   model changed (see the pose README). With the fall toolkit
   enabled the Monitor shows **Running on NPU (core pose)** and the inference time.
   If `init_runtime` reports a permission error, the user running Ember may need to be in
   the `render` group (`sudo usermod -aG render $USER`, then log in again).
7. **Force the CPU** at any time with `BAYMAX_POSE_BACKEND=cpu` in `Baymax-main/.env`
   (or delete `.models/pose_npu`). `BAYMAX_POSE_NPU_CORE=1` moves pose to another NPU core.

See [fall toolkit](features/toolkits/FALL_DETECTION.md) for model/algorithm provenance, synthetic checks, known limits and the real-device test protocol. Full conversation/fall integration must be measured on the board before approval or deployment.
