"""Slow or failed pose inference must not stall capture or replay fall events."""
import collections
import contextlib
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from features.pose_detection.vision_process import LatestVision, Sample
from test_public_core_integration import load_function
from test_toolkit_fall_detection import pose
from features.pose_detection.fall_detection.detector import FallDetector
from features.pose_detection.worker import FallWorker
import os


class ParallelVisionTests(unittest.TestCase):
    def test_v10_builtin_does_not_load_a_duplicate_optional_model(self):
        factory = Mock()
        worker = FallWorker(backend_factory=factory)
        with patch.dict(os.environ, {"BAYMAX_FALL_DETECTION_ENABLED": "1"}):
            worker.start(builtin=True)
            worker.submit(Mock())
        factory.assert_not_called()
        self.assertIsNone(worker.thread)
        self.assertEqual(worker.status()['state'], 'Using built-in v10 fall detection')

    def test_model_loading_and_slow_inference_keep_only_the_latest_frame(self):
        loading, loaded, detecting, release, second = (threading.Event() for _ in range(5))
        calls = []

        def detect(frame):
            calls.append(frame)
            if len(calls) == 1:
                detecting.set()
                self.assertTrue(release.wait(2))
            else:
                second.set()
            return [frame]

        backend = SimpleNamespace(detect=detect, close=Mock())

        def factory():
            loading.set()
            self.assertTrue(loaded.wait(2))
            return backend

        worker = LatestVision(factory)
        try:
            self.assertTrue(loading.wait(1))
            worker.submit(1, 1.0)
            worker.submit(2, 2.0)
            loaded.set()
            self.assertTrue(detecting.wait(1))
            # These submissions complete while inference is still blocked.
            for frame in range(3, 100):
                worker.submit(frame, float(frame))
            self.assertIsNone(worker.poll())
            release.set()
            self.assertTrue(second.wait(1))
            with worker.condition:
                self.assertTrue(worker.condition.wait_for(lambda: worker.result is not None and worker.result.frame == 99, 1))
            self.assertEqual(calls, [2, 99])
            sample = worker.poll()
            self.assertEqual((sample.captured_at, sample.landmarks), (99.0, [99]))
            self.assertIsNone(worker.poll())
            self.assertGreater(worker.dropped, 90)
        finally:
            loaded.set()
            release.set()
            worker.close()
        backend.close.assert_called_once()
        self.assertFalse(worker.thread.is_alive())

    def test_inference_failure_is_reported_and_cleans_up(self):
        backend = SimpleNamespace(detect=Mock(side_effect=RuntimeError("model failed")), close=Mock())
        worker = LatestVision(lambda: backend)
        worker.submit(1, time.monotonic())
        worker.thread.join(1)
        self.assertEqual(worker.error, "RuntimeError: model failed")
        self.assertIsNone(worker.poll())
        worker.submit(2, time.monotonic())
        self.assertIsNone(worker.pending)
        worker.close()
        backend.close.assert_called_once()

    def test_load_failure_does_not_raise_on_camera_thread(self):
        worker = LatestVision(Mock(side_effect=ImportError("missing model")))
        worker.thread.join(1)
        worker.submit(1, time.monotonic())
        self.assertEqual(worker.error, "ImportError: missing model")
        worker.close()

    def test_capture_timestamps_preserve_fall_motion_despite_variable_inference_delays(self):
        detector = FallDetector(confirmation_frames=2)
        frames = [(i / 10, 0) for i in range(13)] + [(1.3, 70), (1.4, 85)]
        frames += [(i / 10, 90) for i in range(15, 36)]
        results = []
        for captured_at, angle in frames:
            with patch('features.pose_detection.fall_detection.detector.time.monotonic', return_value=1000 + captured_at * 7):
                results.append(detector.update(pose(angle), timestamp=captured_at))
        self.assertTrue(results[14]['fall_pending'])
        self.assertEqual(sum(result['fall_detected'] for result in results), 1)
        self.assertFalse(results[33]['fall_detected'])
        self.assertTrue(results[34]['fall_detected'])

    def test_camera_loop_keeps_delivering_frames_without_repeating_an_alert(self):
        self.run_camera_loop()

    def test_camera_loop_survives_pose_failure(self):
        self.run_camera_loop(error="model unavailable")

    def test_camera_loop_rejects_stale_pose_results(self):
        self.run_camera_loop(stale=True)

    def run_camera_loop(self, error=None, stale=False):
        shutdown = threading.Event()
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        reads = []

        def read():
            reads.append(1)
            if len(reads) == 4:  # Includes the initial camera probe.
                shutdown.set()
            return True, frame

        camera = SimpleNamespace(isOpened=lambda: True, read=read, set=Mock(), release=Mock())
        sample = Sample(frame, [1], time.monotonic() - (2 if stale else 0), time.monotonic())
        vision = SimpleNamespace(submit=Mock(), poll=Mock(side_effect=[sample, None, None]), error=error)
        result = dict(fall_detected=True, fall_active=True, fall_pending=False,
                      trunk_angle=80., angular_vel=90., hip_descent_vel=.4)
        detector = SimpleNamespace(update=Mock(return_value=result), _suspicious_streak=0)
        worker = SimpleNamespace(start=Mock(), submit=Mock(), stop=Mock())
        namespace = dict(POSE_TRACKING_ENABLED=True, FALL_DETECTION_ENABLED=True,
                         _ensure_pose_model=Mock(), FallDetector=Mock(return_value=detector),
                         os=SimpleNamespace(getenv=lambda name: "pc"), cv2=SimpleNamespace(
                             CAP_PROP_FRAME_WIDTH=1, CAP_PROP_FRAME_HEIGHT=2),
                         fall_worker=worker, time=time, collections=collections, contextlib=contextlib,
                         LatestVision=lambda factory: contextlib.nullcontext(vision), _POSE_MODEL_PATH="unused",
                         _shutdown_event=shutdown, _latest_frame_lock=threading.Lock(),
                         _update_face_visibility=Mock(), _stream_viewers=0, np=np,
                         _fall_detected_event=threading.Event(), monitor=SimpleNamespace(notify=Mock(), record=Mock()),
                         _notify_webapp=Mock(), FALL_DBG_INTERVAL_S=1.)
        for key in ('ANGLE_THRESHOLD', 'ANG_VEL_THRESHOLD', 'HIP_VEL_THRESHOLD', 'HISTORY_FRAMES',
                    'CONFIRMATION_FRAMES', 'COOLDOWN_SECONDS', 'STAY_DOWN_SECONDS'):
            namespace['FALL_' + key] = 1
        namespace['FALL_DETECTION_FPS'] = 1000
        function = load_function('_fall_detection_thread', namespace)
        with patch('pc_hardware.camera_candidates', return_value=[0]), patch('pc_hardware.open_camera', return_value=camera):
            function()
        self.assertEqual(worker.submit.call_count, 3)
        self.assertEqual(vision.submit.call_count, 3)
        self.assertIs(namespace['_latest_frame'], frame)
        if stale:
            detector.update.assert_not_called()
            namespace['_notify_webapp'].assert_not_called()
        else:
            detector.update.assert_called_once_with([1], timestamp=sample.captured_at)
            namespace['_notify_webapp'].assert_called_once()
        camera.release.assert_called_once()
        worker.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
