"""Camera models run in a child process: frames in, plain landmarks out, errors survive."""
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pose_detection.vision_process import Point, Remote, LatestVision

FAKE = textwrap.dedent('''
    from types import SimpleNamespace

    class Model:
        def detect(self, frame):
            print("library chatter on stdout")  # Must not corrupt the reply pipe.
            if frame == "boom":
                raise ValueError("bad frame")
            if frame is None:
                return None
            return [SimpleNamespace(x=frame, y=0.5, z=0.0, visibility=None, presence=0.9)]

    class Broken:
        def __init__(self):
            raise ImportError("no mediapipe")

    class Slow(Model):
        def detect(self, frame):
            import time
            time.sleep(10)
            return super().detect(frame)
''')


class VisionProcessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        Path(directory.name, "fake_vision_model.py").write_text(FAKE)
        old = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = directory.name + (os.pathsep + old if old else "")
        self.addCleanup(lambda: os.environ.__setitem__("PYTHONPATH", old) if old else os.environ.pop("PYTHONPATH"))
        sys.path.insert(0, directory.name)
        self.addCleanup(sys.path.remove, directory.name)
        import fake_vision_model
        self.fake = fake_vision_model

    def test_landmarks_come_back_as_plain_points_and_errors_do_not_stop_the_process(self):
        with Remote(self.fake.Model) as model:
            self.assertEqual(model.detect(0.25), [Point(0.25, 0.5, 0.0, 0.0, 0.9)])
            self.assertIsNone(model.detect(None))
            with self.assertRaisesRegex(RuntimeError, "bad frame"):
                model.detect("boom")
            self.assertEqual(model.detect(0.75)[0].x, 0.75)
        self.assertIsNotNone(model.child.poll())  # Closed: the child has exited.

    def test_a_model_that_cannot_load_raises_and_leaves_no_process(self):
        with self.assertRaisesRegex(RuntimeError, "no mediapipe"):
            Remote(self.fake.Broken)

    def test_stalled_inference_is_bounded_and_child_is_reaped(self):
        with Remote(self.fake.Slow, timeout=0.1) as model:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                model.detect(0.25)
        self.assertIsNotNone(model.child.poll())

    def test_latest_worker_shutdown_interrupts_a_stalled_child(self):
        import time
        import threading
        model = Remote(self.fake.Slow)
        entered = threading.Event()
        original_detect = model.detect

        def detect(frame):
            entered.set()
            return original_detect(frame)

        model.detect = detect
        worker = LatestVision(lambda: model)
        try:
            worker.submit(0.25, time.monotonic())
            self.assertTrue(entered.wait(1))
        finally:
            worker.close()
        self.assertFalse(worker.thread.is_alive())
        self.assertIsNotNone(model.child.poll())


if __name__ == "__main__":
    unittest.main()
