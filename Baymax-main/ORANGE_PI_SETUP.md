# Orange Pi 5 Max: Debian 13, XFCE/Xorg (experimental)

This is a native Linux ARM64 CPU profile (optional NPU pose: see below). On an Orange Pi 5 Max with Armbian Debian 13 (vendor kernel 6.1), `requirements-orange-pi.txt` installed (Praat-Parselmouth and miniaudio build from source, about 30 minutes; that was before memory's packages and rknn-toolkit-lite2 joined the file: lite2 2.3.2 was installed there by hand, the memory packages not yet, and the current file resolves for aarch64 Python 3.12) and the offline suite passed (`check_release.py` at commit 61c410e: core 352 tests, 15 of them skipped, 0 failures; toolkit 158, 0 skipped). By the counts the 15 skipped are the 14 git-based updater tests (git was not installed on the board) and the off-RK3588 default test, so the updater's tests have not run on the board yet (install git and rerun; confirm the skips with a verbose run). Still to be checked on the board: Ember running live (it has not been started there yet, on the CPU or the NPU: audio, camera, conversation, the live view, the fall toolkit's status; the camera loop's FPS and frame age with the 1-frame camera buffer below; with the NPU: the backend inside Ember end to end), the updater's tests, thermals over a long run, and fall accuracy at home.

1. Extract the complete current branch ZIP into a permanent folder on the Pi.
2. Open devops/clickables/Linux. On a minimal Debian 13 system, run **0 - Debian Prerequisites** first. It lists system libraries/build tools, asks permission, and uses sudo/pkexec for apt installation. It does not install NPU drivers, replace the kernel or configure an audio server. Ensure XFCE desktop audio and a browser already work.
3. Open **1 - Setup**. It installs private Python 3.12 and automatically selects requirements-orange-pi.txt for Linux aarch64/arm64. Expect a potentially long native build for Praat-Parselmouth. A resolver pass does not prove that compilation will succeed.
4. Enter your own Gemini key, select/test mic, speaker and camera, and optionally enable **experimental fall detection (CPU, or NPU on a set-up Orange Pi)**. Save. It is off by default and requires a camera. With camera pose tracking on (the default) it reuses Ember's own pose, which can run on the NPU (below).
5. Open **2 - Monitor**, then **3 - Start Ember**. Ember downloads its pose model (`pose_landmarker_full.task`) on the first camera start. With pose tracking on (the default) the fall toolkit loads no model of its own and the Monitor shows **Running on CPU (core pose)** (or **Running on NPU (core pose)** after the NPU steps below), with actual FPS and inference milliseconds. Only with `camera.pose_tracking = false` does the toolkit's first run download and verify its own model and show **Running on CPU**. Disabled/starting/loading/unavailable/waiting is not a working detector.
6. Use **4 - Stop Ember**. Test device release and restart; keep your evidence for review.

If XFCE opens `.sh` files in an editor, use its file permissions/execution options to run them in a terminal; executing scripts depends on Thunar settings. The current launcher is not a packaged XFCE desktop application. Localized Desktop-folder layouts, USB/CSI camera drivers, user device permissions and sound-server routing can require OS-specific setup.

The prerequisite script is only for Debian 13. It installs curl/certificates, PortAudio, GL/EGL/GLib and sound-file libraries, ALSA plugins, compiler/CMake/pkg-config tools, xdg-utils and XFCE Terminal. It also offers Firefox ESR in the displayed package list when no common browser command is found. This system-package step is separate from the private Python environment and requires administrator authorization. NPU support is not installed by it (see Optional: pose on the NPU). A 64-bit OS and native ARM64 Python are required; 32-bit armhf is not supported by this profile.

## Fast cores

The RK3588 has four fast Cortex-A76 cores (cpu4-7) and four slow Cortex-A55s (cpu0-3).
Ember keeps itself on the fast ones (it prints `[CPU] Running on the fast cores
4,5,6,7`): MediaPipe pose takes about 43 ms a frame on an A76 and over 200 ms on an
A55. `run_mvp.py` (the MVP service) and `run_pc.py` (**3 - Start Ember**) do this first
thing, before the eyes, the network watch and numpy's thread pool start, and also move
any thread that already runs, so every thread of Ember stays on the A76s. Set `BAYMAX_CPU_PINNING=off` in `.env` to
let Linux use all eight. Only Linux on 64-bit ARM with fast and slow cores is affected:
Windows, macOS and x86 Linux PCs (including Intel/AMD hybrid CPUs) are left alone. On
this board the camera also keeps only its newest frame (a V4L2 buffer of one); that is
not measured with a camera yet: check that the FPS stays near 15 (the Monitor shows it
with Setup's fall-detection checkbox on; otherwise read the FPS overlay in the live view).

## Memory

Ember remembers past conversations here as on the robot image. When a session ends,
Gemini summarises it (the same step on every platform) and the summary lines are
stored on the board in `chroma_store/` (in `BAYMAX_DATA_DIR`, by default next to the
code). On each turn the closest memories are looked up and given to Gemini.
`requirements-orange-pi.txt` installs ChromaDB, ONNX Runtime and `tokenizers` for this,
without torch: `semantic_embedder.py` runs the same MiniLM model with the same results
as the robot image.

On its first start Ember downloads the model into `all-MiniLM-L6-v2-onnx/` in the
background: about 90 MB from huggingface.co, pinned to one revision and checked by
SHA-256. Ember starts and talks meanwhile, and memory turns on when the download
finishes (a first start has nothing to recall yet). The log shows `[MEMORY]
Downloading the memory model ...`, then `[MEMORY] Embedder ready — N memories in
store`. To fetch it beforehand, run `.venv/bin/python semantic_embedder.py --download`
from `Baymax-main`. If the download fails (no network), memory is off for that session
and the download is tried again on the next start.

Measured on a Mac with this profile's package versions: about 250-280 MB more RAM with
500 memories stored, and 4-11 ms per lookup. On the board: still to be measured.

An Orange Pi installed before this change needs the new packages once:
`.venv/bin/python -m pip install -r requirements-orange-pi.txt` (or rerun **1 - Setup**).

`retrieval_enabled = false` under `[memory]` in `/etc/ember/device.toml` turns off the
lookups, and then nothing downloads the model: run `--download` (above) once, or
session summaries can't be saved.

## Optional: pose on the NPU

Ember runs MediaPipe Pose on the CPU unless the RK3588's NPU is ready; then the camera
thread's pose (fall detection, face presence, the live view and the fall toolkit) uses
the NPU with the same model weights ([features/pose](features/pose/README.md)). Nothing
changes until all of the steps below are done, and any failure falls back to the CPU.
Steps 1-5 were run on an Orange Pi 5 Max with Armbian Debian 13 (vendor kernel
6.1.172, RKNPU 0.9.8) on 2026-10-28, with Ember stopped, including step 5's paced
`--fps 15` and `--empty` runs; see features/pose/VALIDATION.md for the results. Step 6
with Ember running is still pending on the board (the backend choice was confirmed
there by calling `create_pose_backend`).
The rknn converter prints a few `E RKNN: ... Unkown op target: 0` lines while building
and the runtime a `query RKNN_QUERY_INPUT_DYNAMIC_RANGE error` warning when it loads a
model: both are harmless (the check and the models are fine).

1. **Check the kernel, the NPU driver and access to the NPU.** RKNN needs Rockchip's vendor kernel (6.1 series)
   with the `rknpu` driver (0.9.8 is current); a mainline kernel's `rocket` driver does
   not work with RKNN.

   ```bash
   uname -r                                          # vendor kernels look like 6.1.x-...rk35xx / -rockchip-rk3588
   ls /sys/bus/platform/drivers | grep -i -e rknpu -e rocket   # need: RKNPU
   sudo cat /sys/kernel/debug/rknpu/version          # e.g. RKNPU driver: v0.9.8
   tr '\0' '\n' < /proc/device-tree/compatible       # need: rockchip,rk3588
   ls -l /dev/dri/by-path/ | grep -i npu             # the NPU's device nodes, e.g. card1 and renderD129
   ls -l /dev/dri/                                   # their group (video / render) and mode
   id -nG                                            # the groups of the user who runs Ember
   ```

   No `RKNPU` means this image cannot use the NPU: keep the CPU (nothing to do) or move
   to an image with the vendor kernel. Changing the kernel is outside Ember's setup.
   The user who runs Ember (and the converter) needs read/write access to the NPU's
   nodes. On the tested image they are `card1` (group `video`) and `renderD129` (group
   `render`), mode 0660, and the default Armbian user is in both groups. If yours is
   not: `sudo usermod -aG render,video $USER`, then log out and in again.
2. **Install the NPU runtime library (administrator step).** Use the same version as
   step 3 (2.3.2 here; 2.5.0 SHA-256 `4b6cc575bedf8952b13db4b1190fa3b3d2ac213f00be774ff6ccd19d651970c0`):

   ```bash
   curl -fLO https://github.com/airockchip/rknn-toolkit2/raw/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so
   echo "d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8  librknnrt.so" | sha256sum -c
   sudo install -m 0644 librknnrt.so /usr/lib/librknnrt.so && sudo ldconfig
   ```
3. **rknn-toolkit-lite2 (the NPU's Python side) comes with `requirements-orange-pi.txt`**
   (2.3.2, the converter's default; Python 3.12 aarch64 wheel), so **1 - Setup** and
   updates install it. On a Pi set up before it was added, rerun **1 - Setup** or:

   ```bash
   cd Baymax-main
   .venv/bin/python -m pip install -r requirements-orange-pi.txt
   ```

   Ember only uses the NPU on its own when the accuracy check (step 4) ran with the same
   rknn-toolkit-lite2 version. The converter's `--rknn-version 2.5.0` is for
   experiments: Setup and updates put 2.3.2 back, and Ember then logs the version
   mismatch and uses the CPU until the check is re-run with 2.3.2.
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
   visible) on still frames and on a 72-frame tracked sequence (including a fall); it
   checks that the conversion is faithful, not how the NPU does on real footage. To
   repeat only the check: `.models/pose_npu/build/venv-rknn/bin/python
   features/pose/convert_rknn.py check`. Don't add `--video` to it: a real clip becomes
   part of the gate, whose limits are set for the synthetic sequence, and on the tested
   board every real clip tried missed them, which keeps Ember on the CPU (a plain
   `check` restores the NPU). To see how the NPU does on your own footage, compare it
   with MediaPipe instead (Ember stopped; the clip stays on the board):
   `.venv/bin/python -m features.pose.validate --engine npu --task pose_landmarker_full.task --onnx-dir .models/pose_npu --fps 15 my_clip.mp4`.
   A check that fails with `error during the check: RuntimeError: init_runtime(...)
   returned -1` usually means the NPU device could not be opened: see the access check
   in step 1 (librknnrt may print `failed to open rknpu module, need to insmod rknpu
   dirver!` even when the driver is loaded), or a driver problem.
5. **Measure** (Ember still stopped, so it does not compete for the CPU and NPU):
   `.venv/bin/python features/pose/benchmark.py` prints milliseconds and process CPU
   per frame for MediaPipe on the CPU and for the NPU, and how close their landmarks
   are. By default frames run back to back (throughput: about 11 ms on the NPU here);
   `--fps 15` paces them like Ember, where an NPU frame takes longer (measured here:
   median 16.2 ms with a person in view, 28.5 ms with nobody in view, `--empty`, against
   about 40-44 ms for MediaPipe) and process CPU is 12-20% of a core instead of 70-77%.
   Process CPU counts every thread while each frame's pose runs, not between frames.
6. **Confirm it is active.** Start Ember. The log shows
   `[POSE] Pose backend: NPU (RK3588) on NPU core 0 (...)`; otherwise
   `[POSE] Pose backend: CPU (MediaPipe). NPU skipped: <reason>`. If the `librknnrt` or
   rknpu driver version changed since the check (e.g. a kernel update that brought a new
   rknpu driver) the reason says so: re-run the check. A kernel update that keeps both
   versions keeps the NPU without a new check; re-running the check after any kernel
   update does no harm. A `[POSE] Warning:` line after the NPU line means the downloaded
   MediaPipe model changed (see the pose README). With the fall toolkit enabled the
   Monitor shows **Running on NPU (core pose)** and the inference time, the paced figure
   (about 16-29 ms, higher than the benchmark's back-to-back number). If the reason is
   `NPU init failed (... init_runtime ...)` and the check passed as another user, the
   user running Ember lacks access to the NPU's nodes: see step 1.
7. **Force the CPU** at any time with `BAYMAX_POSE_BACKEND=cpu` (or `mediapipe`) in
   `Baymax-main/.env` (or delete `.models/pose_npu`). Any other unrecognised value also
   means the CPU, with a warning. `BAYMAX_POSE_NPU_CORE=1` moves pose to another NPU core.

See [fall toolkit](features/toolkits/FALL_DETECTION.md) for model/algorithm provenance, synthetic checks, known limits and the real-device test protocol. Full conversation/fall integration must be measured on the board before approval or deployment.
