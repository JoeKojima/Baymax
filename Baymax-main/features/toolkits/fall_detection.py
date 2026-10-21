"""Opt-in CPU pose/fall toolkit. Reuses camera frames; never opens a camera."""
import asyncio
from collections import deque
import hashlib
import os
from pathlib import Path
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task"
MODEL_SHA256 = "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"


# Other local features that reuse the camera frames / live session (e.g. proactive greetings).
frame_observers = []   # observer(frame), called on the camera thread; must be cheap.
# async task(session, shutdown_event), run for each live session. shutdown_event is a
# threading.Event: poll is_set() and await asyncio.sleep(); never call its wait().
session_tasks = []


def enabled():
    return os.getenv("BAYMAX_FALL_DETECTION_ENABLED", "0") == "1"


def ensure_model(path, fetch=None, expected_hash=MODEL_SHA256):
    path = Path(path)
    if path.is_file():
        data = path.read_bytes()
    else:
        if fetch is None:
            def fetch():
                with urllib.request.urlopen(MODEL_URL, timeout=30) as response:
                    return response.read(12 * 1024 * 1024 + 1)
        data = fetch()
        if len(data) > 12 * 1024 * 1024:
            raise RuntimeError("Pose model download exceeded its expected size.")
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise RuntimeError("Pose model checksum failed. Remove the cached .models file and retry; detection is unavailable.")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".download")
        temporary.write_bytes(data)
        temporary.replace(path)
    return path


class CpuPose:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks import python as tasks
        from mediapipe.tasks.python import vision
        self.mp = mp
        model = ensure_model(ROOT / ".models/pose_landmarker_full.task")
        options = vision.PoseLandmarkerOptions(
            base_options=tasks.BaseOptions(model_asset_path=str(model), delegate=tasks.BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO, num_poses=1,
            min_pose_detection_confidence=0.5, min_pose_presence_confidence=0.5, min_tracking_confidence=0.5)
        self.landmarker = vision.PoseLandmarker.create_from_options(options)
        self.timestamp = -1

    def detect(self, frame):
        import cv2
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        self.timestamp = max(self.timestamp + 1, int(time.monotonic() * 1000))
        result = self.landmarker.detect_for_video(image, self.timestamp)
        return result.pose_landmarks[0] if result.pose_landmarks else None

    def close(self):
        self.landmarker.close()


def _detector_factory(**settings):
    from fall_detection.detector import FallDetector
    return FallDetector(**settings)


def _record(message):
    import runtime_monitor
    runtime_monitor.record("fall_detection", message)


class FallWorker:
    def __init__(self, backend_factory=CpuPose, detector_factory=_detector_factory):
        self.backend_factory = backend_factory
        self.detector_factory = detector_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.pending = None
        self.alerts = deque(maxlen=8)
        self.thread = None
        self.is_enabled = False
        self.state = "Disabled"
        self.inference_ms = None
        self.last_result = None
        self.samples = deque(maxlen=30)
        self.dropped = 0

    def start(self, fps=15, **settings):
        if self.thread and self.thread.is_alive():
            return
        self.is_enabled = enabled()
        if not self.is_enabled:
            self.state = "Disabled"
            return
        try:
            fps = float(os.getenv("BAYMAX_FALL_FPS", str(fps)))
        except ValueError:
            fps = 0
        if not 2 <= fps <= 30:
            self.state = "Unavailable: target FPS must be between 2 and 30"
            _record(self.state)
            return
        self.interval = 1.0 / fps
        self.settings = settings
        self.stop_event.clear()
        self.state = "Loading CPU pose model"
        self.thread = threading.Thread(target=self._run, name="ember-fall-cpu", daemon=True)
        self.thread.start()

    def submit(self, frame):
        import runtime_monitor
        runtime_monitor.sensor_alive('camera')  # Every good frame passes here, fall detection on or off.
        for observer in list(frame_observers):
            try:
                observer(frame)
            except Exception:
                frame_observers.remove(observer)
                print("[CAM] Frame observer disabled after an error.", flush=True)
        if not self.is_enabled or self.stop_event.is_set() or not self.thread or not self.thread.is_alive():
            return
        # Keep one frame, not a growing inference queue. Copy outside camera locks.
        item = (time.monotonic(), frame.copy())
        with self.lock:
            if self.pending is not None:
                self.dropped += 1
            self.pending = item
        self.wake.set()

    def _run(self):
        backend = None
        try:
            backend = self.backend_factory()
            detector = self.detector_factory(**self.settings)
            self.state = "Running on CPU"
            _record("Fall detection running on CPU; development mode")
            next_frame = 0.0
            while not self.stop_event.is_set():
                self.wake.wait(timeout=0.2)
                self.wake.clear()
                if self.stop_event.wait(max(0, next_frame - time.monotonic())):
                    break
                with self.lock:
                    item, self.pending = self.pending, None
                if item is None or time.monotonic() - item[0] > 0.5:
                    continue
                started = time.monotonic()
                landmarks = backend.detect(item[1])
                if self.stop_event.is_set():
                    break
                result = detector.update(landmarks)
                finished = time.monotonic()
                with self.lock:
                    self.inference_ms = round((finished - started) * 1000, 1)
                    self.samples.append(finished)
                    self.last_result = {k: result.get(k) for k in ("fall_pending", "fall_active", "trunk_angle")}
                    if result.get("fall_detected"):
                        self.alerts.append({"time": time.time(), "recorded": finished})
                if result.get("fall_detected"):
                    _record("Possible fall detected locally; awaiting a check-in. No external alert sent.")
                next_frame = started + self.interval
        except Exception as error:
            self.state = "Unavailable: " + type(error).__name__
            _record("Fall detection unavailable (" + type(error).__name__ + "). Conversation can continue; check the fall toolkit setup.")
            print("[FALL] Detection unavailable: " + str(error), flush=True)
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            if not self.state.startswith("Unavailable"):
                self.state = "Stopped"

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        with self.lock:
            self.pending = None
            self.alerts.clear()

    def pop_alert(self):
        with self.lock:
            while self.alerts:
                alert = self.alerts.popleft()
                if time.monotonic() - alert["recorded"] <= 15:
                    return alert
        return None

    def status(self):
        with self.lock:
            fps = ((len(self.samples) - 1) / (self.samples[-1] - self.samples[0])
                   if len(self.samples) > 1 and self.samples[-1] > self.samples[0] else None)
            state = self.state
            if state == "Running on CPU" and (not self.samples or time.monotonic() - self.samples[-1] > 2):
                state = "Waiting for fresh camera frames"
                fps = None
            if state == "Disabled" and enabled():
                state = "Waiting for camera"
            if self.stop_event.is_set() and self.is_enabled and self.thread and self.thread.is_alive():
                state = "Stopping"
            return {"state": state, "backend": "CPU", "inference_ms": self.inference_ms,
                    "fps": round(fps, 1) if fps is not None else None, "dropped": self.dropped,
                    "last_result": dict(self.last_result) if self.last_result else None}

    async def alert_monitor(self, session, shutdown_event):
        tasks = [asyncio.create_task(task(session, shutdown_event)) for task in list(session_tasks)]
        try:
            await self._alerts(session, shutdown_event)
        finally:
            for task in tasks:
                task.cancel()

    async def _alerts(self, session, shutdown_event):
        while not shutdown_event.is_set():
            alert = self.pop_alert()
            if alert:
                import runtime_monitor
                runtime_monitor.notify('fall_alert')
                try:
                    await session.send_client_content(
                        turns={"role": "user", "parts": [{"text": "[Local experimental fall detector] A possible fall was detected. "
                               "Check in calmly and ask whether I am okay. This is uncertain; do not claim an emergency "
                               "or that anyone has been contacted."}]}, turn_complete=True)
                except Exception:
                    _record("Possible fall check-in could not reach Gemini. The local event remains in the monitor.")
            await asyncio.sleep(0.1)


worker = FallWorker()
