"""Choose the core camera thread's pose backend.

    pose = create_pose_backend(mediapipe_options=options)
    landmarks = pose.detect(frame_bgr, timestamp_ms)   # None or 33 landmarks
    pose.name    -> "NPU (RK3588)" / "CPU (MediaPipe)" / "CPU (ONNX Runtime)"
    pose.device  -> "NPU" / "CPU"
    pose.close()

BAYMAX_POSE_BACKEND=auto (default) uses the RK3588 NPU only when all of these hold:
Linux aarch64; 'rockchip,rk3588' in /proc/device-tree/compatible; rknn-toolkit-lite2
imports; the converted .rknn files exist with the SHA-256s recorded at conversion and
the converter's on-board accuracy check passed; init_runtime and a warm-up inference
succeed. Otherwise it is MediaPipe on the CPU, exactly as before (same options, same
calls). =cpu forces MediaPipe; =npu skips the board/accuracy-check gates (still needs
rknnlite, matching files and a working runtime) and =onnx runs the port on ONNX Runtime
(development). Every choice is printed once with the reason the NPU was skipped. If
the NPU fails during use, the backend switches to MediaPipe for the rest of the run.
"""
import os
import platform
import sys

from features.pose import models

ENV_BACKEND = "BAYMAX_POSE_BACKEND"
ENV_CORE = "BAYMAX_POSE_NPU_CORE"
CHOICES = ("auto", "cpu", "npu", "onnx")
NPU_NAME = "NPU (RK3588)"
CPU_NAME = "CPU (MediaPipe)"
ONNX_NAME = "CPU (ONNX Runtime)"


def _log(message):
    print(f"[POSE] {message}", flush=True)


def default_mediapipe_options(model_path):
    """The runtime's PoseLandmarker settings (realtime_gemini_10.py passes its own)."""
    from mediapipe.tasks import python as mp_tasks
    from mediapipe.tasks.python import vision
    return vision.PoseLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.VIDEO, num_poses=1,
        min_pose_detection_confidence=0.5, min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5)


class _Backend:
    name = ""
    device = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class MediaPipeCpuBackend(_Backend):
    """MediaPipe PoseLandmarker on the CPU: the calls the camera thread always made."""
    name = CPU_NAME
    device = "CPU"

    def __init__(self, options):
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        self._mp = mp
        self._landmarker = vision.PoseLandmarker.create_from_options(options)

    def detect(self, frame_bgr, timestamp_ms):
        import cv2
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        detection = self._landmarker.detect_for_video(mp_image, timestamp_ms)
        return detection.pose_landmarks[0] if detection.pose_landmarks else None

    def close(self):
        self._landmarker.close()


class PipelineBackend(_Backend):
    """features.pose.pipeline on an engine (RKNN on the NPU, or ONNX Runtime)."""

    def __init__(self, engine, name, device):
        from features.pose.pipeline import PosePipeline
        self.engine = engine
        self.name = name
        self.device = device
        self.pipeline = PosePipeline(engine)

    def detect(self, frame_bgr, timestamp_ms):
        return self.pipeline.detect(frame_bgr, timestamp_ms)

    def close(self):
        self.engine.close()


class FallbackBackend(_Backend):
    """The NPU backend, replaced by MediaPipe on the CPU if it ever raises."""

    def __init__(self, primary, cpu_factory, log=_log):
        self.active = primary
        self.cpu_factory = cpu_factory
        self.log = log

    @property
    def name(self):
        return self.active.name

    @property
    def device(self):
        return self.active.device

    def detect(self, frame_bgr, timestamp_ms):
        try:
            return self.active.detect(frame_bgr, timestamp_ms)
        except Exception as error:
            if self.cpu_factory is None:
                raise
            failed = self.active
            self.log(f"{failed.name} failed during inference ({type(error).__name__}: {error}); "
                     f"switching to {CPU_NAME} for the rest of this run.")
            self.cpu_factory, factory = None, self.cpu_factory
            try:
                failed.close()
            except Exception:
                pass
            self.active = factory()
            return self.active.detect(frame_bgr, timestamp_ms)

    def close(self):
        self.active.close()


# ── Probes (replaced in tests) ───────────────────────────────────────────────
def on_rk3588():
    import pc_hardware
    return pc_hardware.is_rk3588()


def import_rknnlite():
    from features.pose.engines import rknnlite_class   # reached only after the platform checks
    return rknnlite_class()


def driver_hint():
    """Kernel/driver facts worth printing when the NPU will not start."""
    import pc_hardware
    drivers = pc_hardware.npu_drivers()
    if any(name.lower() == "rknpu" for name in drivers):
        driver = "rknpu driver loaded"
    elif drivers:
        driver = f"only the mainline '{drivers[0]}' NPU driver (RKNN needs Rockchip's vendor rknpu driver)"
    else:
        driver = "no rknpu driver (RKNN needs Rockchip's vendor kernel, e.g. 6.1 with rknpu 0.9.8)"
    return f"kernel {platform.release()}; {driver}"


def npu_unavailable_reason(env=None, *, system=None, machine=None, rk3588=on_rk3588,
                           rknnlite=import_rknnlite, forced=False):
    """None when the NPU may be tried, else a one-line reason. Imports rknnlite only on
    Linux aarch64 (and only on an RK3588 unless forced)."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if not system.startswith("linux") or machine not in ("aarch64", "arm64"):
        return f"not Linux aarch64 ({system} {machine})"
    if not forced and not rk3588():
        return "not an RK3588 board ('rockchip,rk3588' not in /proc/device-tree/compatible)"
    try:
        rknnlite()
    except Exception as error:
        return f"rknn-toolkit-lite2 is not usable in this Python ({type(error).__name__}: {error})"
    directory = models.model_dir(env)
    try:
        manifest = models.load_manifest(directory)
        models.verified_files(directory, manifest, "rknn")
    except models.ModelError as error:
        return str(error)
    if not forced and not models.check_passed(manifest):
        return "the converter's on-board accuracy check has not passed (see manifest.json; re-run convert_rknn.py)"
    return None


def _open_npu(env, engine_factory):
    directory = models.model_dir(env)
    manifest = models.load_manifest(directory)
    paths = models.verified_files(directory, manifest, "rknn")
    core = env.get(ENV_CORE, "0").strip() or "0"
    engine = engine_factory(paths["pose_detector"], paths["pose_landmarks_detector"], core)
    try:
        _warm_up(engine)
    except Exception:
        engine.close()
        raise
    tools = manifest.get("tools") or {}
    return PipelineBackend(engine, NPU_NAME, "NPU"), core, tools.get("rknn-toolkit2", "?")


def _open_onnx(env):
    from features.pose.engines import OnnxEngine
    directory = models.model_dir(env)
    paths = models.verified_files(directory, models.load_manifest(directory), "onnx")
    engine = OnnxEngine(paths["pose_detector"], paths["pose_landmarks_detector"])
    return PipelineBackend(engine, ONNX_NAME, "CPU")


def _warm_up(engine):
    import numpy as np
    boxes, scores = engine.detector(np.zeros((224, 224, 3), np.uint8))
    raw, flag, heatmap = engine.landmarks(np.zeros((256, 256, 3), np.uint8))
    if not (np.isfinite(boxes).all() and np.isfinite(raw).all() and np.isfinite(flag).all()):
        raise RuntimeError("warm-up inference returned non-finite values")


def _rknn_engine(detector, landmarks, core):
    from features.pose.engines import RknnEngine
    return RknnEngine(detector, landmarks, core)


def create_pose_backend(mediapipe_options=None, model_path=None, env=None, log=_log,
                        cpu_factory=None, probe=None, engine_factory=_rknn_engine):
    """The pose backend for this machine (see the module docstring).

    mediapipe_options: the runtime's PoseLandmarkerOptions for the CPU path
    (default: default_mediapipe_options(model_path)). probe/engine_factory/cpu_factory
    are for tests."""
    env = os.environ if env is None else env
    if cpu_factory is None:
        def cpu_factory():
            options = mediapipe_options if mediapipe_options is not None else default_mediapipe_options(model_path)
            return MediaPipeCpuBackend(options)
    probe = probe or npu_unavailable_reason

    choice = env.get(ENV_BACKEND, "auto").strip().lower() or "auto"
    if choice not in CHOICES:
        log(f"Unknown {ENV_BACKEND}={choice!r} (use {'|'.join(CHOICES)}); using auto.")
        choice = "auto"

    if choice == "cpu":
        log(f"Pose backend: {CPU_NAME} ({ENV_BACKEND}=cpu).")
        return cpu_factory()

    if choice == "onnx":
        try:
            backend = _open_onnx(env)
            log(f"Pose backend: {ONNX_NAME} ({ENV_BACKEND}=onnx).")
            return FallbackBackend(backend, cpu_factory, log)
        except Exception as error:
            log(f"Pose backend: {CPU_NAME}. ONNX Runtime skipped: {type(error).__name__}: {error}")
            return cpu_factory()

    forced = choice == "npu"
    reason = probe(env, forced=forced)
    if reason is None:
        try:
            backend, core, toolkit = _open_npu(env, engine_factory)
        except Exception as error:
            reason = f"NPU init failed ({type(error).__name__}: {error}); {driver_hint()}"
        else:
            log(f"Pose backend: {NPU_NAME} on NPU core {core} (models from rknn-toolkit2 {toolkit}"
                f"{'; forced' if forced else ''}). {ENV_BACKEND}=cpu returns to MediaPipe.")
            return FallbackBackend(backend, cpu_factory, log)
    log(f"Pose backend: {CPU_NAME}. NPU skipped: {reason}.")
    return cpu_factory()
