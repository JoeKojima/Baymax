"""Activity tracking, Tier 1: posture rules, segments, store, worker, hooks and devUI.

Poses are built by hand; no camera, model or Gemini is used.
"""
import ast
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.toolkits import fall_detection
from features.toolkits import activity as activity_module
from features.toolkits.activity import Activity, posture, store as store_module, tracker as tracker_module
from features.toolkits.activity.store import Store, timeline
from features.toolkits.activity.tracker import Tracker

ROOT = Path(__file__).resolve().parents[1]
V10 = ROOT / "realtime_gemini_10.py"


def pose(shoulder_y=0.3, hip_y=0.55, knee_y=0.75, ankle_y=0.95, legs=True, torso=True, hand_at_mouth=False):
    """33 image landmarks (x, y, z, visibility) for a front-facing person."""
    points = [(0.5, 0.5, 0.0, 0.0)] * 33
    def put(i, x, y, visible=True):
        points[i] = (x, y, 0.0, 0.9 if visible else 0.1)
    put(0, 0.5, shoulder_y - 0.12)
    put(9, 0.48, shoulder_y - 0.07)
    put(10, 0.52, shoulder_y - 0.07)
    put(11, 0.42, shoulder_y, torso)
    put(12, 0.58, shoulder_y, torso)
    put(23, 0.45, hip_y, torso)
    put(24, 0.55, hip_y, torso)
    put(25, 0.45, knee_y, legs)
    put(26, 0.55, knee_y, legs)
    put(27, 0.45, ankle_y, legs)
    put(28, 0.55, ankle_y, legs)
    wrist_y = shoulder_y - 0.07 if hand_at_mouth else hip_y
    put(15, 0.49 if hand_at_mouth else 0.4, wrist_y)
    put(16, 0.6, hip_y)
    return tuple(points)


def world(shoulder=(0, -0.5, 0), knee=(0, 0.45, 0), ankle=(0, 0.9, 0)):
    """33 world landmarks (metres, hips at the origin, y down)."""
    points = [(0.0, 0.0, 0.0)] * 33
    for side, x in ((0, -0.15), (1, 0.15)):
        points[11 + side] = (shoulder[0] + x, shoulder[1], shoulder[2])
        points[23 + side] = (x / 2, 0.0, 0.0)
        points[25 + side] = (knee[0] + x / 2, knee[1], knee[2])
        points[27 + side] = (ankle[0] + x / 2, ankle[1], ankle[2])
    return tuple(points)


STANDING = (pose(), world())
SITTING = (pose(knee_y=0.6, ankle_y=0.85), world(knee=(0, 0.05, -0.45), ankle=(0, 0.5, -0.45)))
LYING = (pose(shoulder_y=0.6, hip_y=0.62), world(shoulder=(-0.5, 0, 0), knee=(0.45, 0, 0), ankle=(0.9, 0, 0)))


def classify(image, world_points=None):
    return posture.classify(posture.features(image, world_points))


class PostureTests(unittest.TestCase):
    def test_standing_sitting_and_lying_from_world_landmarks(self):
        self.assertEqual(classify(*STANDING), "standing")
        self.assertEqual(classify(*SITTING), "sitting")
        self.assertEqual(classify(*LYING), "lying")

    def test_side_view_sitting_without_world_landmarks(self):
        side = list(pose(knee_y=0.57, ankle_y=0.85))
        side[25] = (0.7, 0.57, 0.0, 0.9)   # Thighs horizontal in the image.
        side[26] = (0.75, 0.57, 0.0, 0.9)
        self.assertEqual(classify(tuple(side)), "sitting")
        self.assertEqual(classify(pose()), "standing")

    def test_hidden_legs_are_upright_and_hidden_torso_is_neutral(self):
        self.assertEqual(classify(pose(legs=False)), "upright")
        self.assertIsNone(posture.features(pose(torso=False)))
        self.assertEqual(posture.classify(None), posture.NEUTRAL)

    def test_one_side_of_the_torso_is_enough(self):
        # Live test: an arm across the body hid a shoulder and gave 'unknown'.
        image = list(STANDING[0])
        image[12] = image[12][:3] + (0.1,)
        image[24] = image[24][:3] + (0.1,)
        self.assertEqual(classify(tuple(image), STANDING[1]), "standing")
        image[11] = image[11][:3] + (0.1,)
        self.assertIsNone(posture.features(tuple(image)))

    def test_hips_away_from_the_frame_edges_are_central(self):
        self.assertTrue(posture.features(pose())["central"])
        at_edge = tuple((x + 0.47, y, z, v) for x, y, z, v in pose())
        self.assertFalse(posture.features(at_edge)["central"])

    def test_hand_near_mouth(self):
        self.assertTrue(posture.features(pose(hand_at_mouth=True))["hand_near_mouth"])
        self.assertFalse(posture.features(pose())["hand_near_mouth"])

    def test_movement_is_in_torso_lengths_per_second(self):
        a = posture.features(pose())
        b = posture.features(tuple((x + 0.025, y, z, v) for x, y, z, v in pose()))
        body, hip = posture.movement(a, b, 0.5)
        self.assertAlmostEqual(hip, (0.025 * 4 / 3) / 0.25 / 0.5, places=3)
        self.assertGreater(body, 0)
        self.assertEqual(posture.movement(None, b, 0.5), (None, None))


def run(tracker, start, seconds, label, rate=5, hip_at=None, **kwargs):
    """Samples at `rate` per second; hip_at(seconds since start) gives the hip position."""
    t = start
    for i in range(int(seconds * rate)):
        t = start + i / rate
        extra = {"hip": hip_at(i / rate), "torso": 0.25} if hip_at else {}
        tracker.sample(t, label, **kwargs, **extra)
    return start + seconds


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.tracker = Tracker(self.closed.append)

    def test_switch_is_backdated_to_when_the_new_posture_began(self):
        t = run(self.tracker, 1000, 60, "standing", body_speed=0.1)
        run(self.tracker, t, 60, "sitting", body_speed=0.01)
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["standing", "sitting"])
        standing, sitting = self.closed
        self.assertAlmostEqual(standing["start"], 1000, delta=0.3)
        self.assertAlmostEqual(standing["end"], 1060, delta=0.3)
        self.assertAlmostEqual(sitting["start"], 1060, delta=0.3)
        self.assertEqual(standing["confidence"], 1.0)
        self.assertEqual(sitting["confidence"], 1.0)
        self.assertEqual(standing["samples"] + sitting["samples"], 600)
        self.assertGreater(sitting["still_share"], 0.9)

    def test_short_blips_do_not_split_a_segment(self):
        t = run(self.tracker, 0, 60, "sitting")
        t = run(self.tracker, t, 5, "lying")
        t = run(self.tracker, t, 60, "sitting")
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["sitting"])
        self.assertLess(self.closed[0]["confidence"], 1.0)

    def test_hidden_legs_keep_the_known_posture(self):
        t = run(self.tracker, 0, 30, "sitting")
        run(self.tracker, t, 60, "upright")
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["sitting"])
        self.assertEqual(self.closed[0]["confidence"], 1.0)

    def test_hidden_legs_use_hip_height_against_the_last_known_posture(self):
        # Live test (2026-11-13): standing behind a chair after sitting stayed "sitting".
        t = run(self.tracker, 0, 30, "sitting", hip_at=lambda s: (0.5, 0.6))
        t = run(self.tracker, t, 40, "upright", hip_at=lambda s: (0.5, 0.4))   # 0.8 torso higher.
        t = run(self.tracker, t, 40, "upright", hip_at=lambda s: (0.5, 0.6))   # Back down.
        run(self.tracker, t, 30, "upright", hip_at=lambda s: (0.5, 0.55))     # Leaning: no change.
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["sitting", "standing", "sitting"])

    def test_hip_height_is_ignored_after_moving_away_or_leaving(self):
        t = run(self.tracker, 0, 30, "sitting", hip_at=lambda s: (0.2, 0.6))
        run(self.tracker, t, 40, "upright", hip_at=lambda s: (0.8, 0.4))   # 2.4 torso lengths sideways.
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["sitting"])
        tracker = Tracker(self.closed.append)
        t = run(tracker, 0, 30, "standing", hip_at=lambda s: (0.5, 0.4))
        t = run(tracker, t, 1, "absent")
        run(tracker, t, 40, "upright", hip_at=lambda s: (0.5, 0.6))
        tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed[1:]], ["standing"])

    def test_lying_person_who_vanishes_mid_frame_is_still_lying(self):
        # Live test (2026-11-13): under a blanket, pose found nobody and it read "not in view".
        t = run(self.tracker, 0, 60, "lying", central=True)
        run(self.tracker, t, 120, "absent")
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["lying"])
        self.assertAlmostEqual(self.closed[0]["end"], 179.8, delta=0.3)
        # Lying at the edge of the view and then gone: they left.
        tracker = Tracker(self.closed.append)
        t = run(tracker, 0, 60, "lying", central=False)
        run(tracker, t, 60, "absent")
        tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed[1:]], ["lying", "absent"])
        # Got up first (seen sitting), then gone: they left.
        tracker = Tracker(self.closed.append)
        t = run(tracker, 0, 60, "lying", central=True)
        t = run(tracker, t, 3, "sitting")
        run(tracker, t, 60, "absent")
        tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed[3:]], ["lying", "absent"])

    def test_neutral_samples_never_switch(self):
        t = run(self.tracker, 0, 30, "standing")
        run(self.tracker, t, 60, posture.NEUTRAL)
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["standing"])

    def test_gap_closes_the_segment_without_inventing_data(self):
        run(self.tracker, 0, 30, "sitting")
        run(self.tracker, 100, 30, "sitting")
        self.tracker.gap()
        self.assertEqual(len(self.closed), 2)
        self.assertAlmostEqual(self.closed[0]["end"], 29.8, delta=0.3)
        self.assertAlmostEqual(self.closed[1]["start"], 100, delta=0.3)

    def test_absent_needs_thirty_seconds(self):
        t = run(self.tracker, 0, 30, "sitting")
        t = run(self.tracker, t, 20, "absent")
        t = run(self.tracker, t, 30, "sitting")
        t = run(self.tracker, t, 40, "absent")
        self.tracker.gap()
        self.assertEqual([s["hint"] for s in self.closed], ["sitting", "not_in_view"])

    def test_standing_with_moving_hips_is_walking(self):
        t = run(self.tracker, 0, 20, "standing", hip_at=lambda s: (0.5, 0.5))
        run(self.tracker, t, 20, "standing", hip_at=lambda s: (0.1 + 0.2 * (s % 4), 0.5))  # 0.8 torso/s
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["standing", "walking"])

    def test_hips_jittering_in_place_are_not_walking(self):
        # Live test: standing still behind a chair read as walking.
        import random
        rng = random.Random(1)
        run(self.tracker, 0, 60, "upright",
            hip_at=lambda s: (0.5 + rng.uniform(-0.04, 0.04), 0.5 + rng.uniform(-0.04, 0.04)))
        self.tracker.gap()
        self.assertEqual([s["posture"] for s in self.closed], ["upright"])

    def test_repeated_hand_to_mouth_while_seated_hints_eating(self):
        t = 0.0
        for second in range(240):
            for k in range(5):
                near = second % 20 == 0 and k < 3   # Three per minute.
                self.tracker.sample(t, "sitting", hand_mouth=0.3 if near else 1.2)
                t += 0.2
        self.tracker.gap()
        segment = self.closed[0]
        self.assertEqual(segment["hand_to_mouth"], 12)
        self.assertEqual(segment["hint"], "possibly_eating_or_drinking")

    def test_a_hand_hovering_at_the_line_counts_once(self):
        # Live test: 8 events counted for 5 movements.
        t = run(self.tracker, 0, 10, "sitting", hand_mouth=1.2)
        for i in range(50):
            self.tracker.sample(t + i / 5, "sitting", hand_mouth=0.45 if i % 2 else 0.65)
        run(self.tracker, t + 10, 30, "sitting", hand_mouth=1.2)
        self.tracker.gap()
        self.assertEqual(self.closed[0]["hand_to_mouth"], 1)

    def test_hand_movements_while_walking_or_lying_do_not_count(self):
        t = 0.0
        for second in range(60):
            for k in range(5):
                self.tracker.sample(t, "lying", hand_mouth=0.3 if k < 2 else 1.2)
                t += 0.2
        self.tracker.gap()
        self.assertEqual(self.closed[0]["hand_to_mouth"], 0)

    def test_talking_hint_and_lying_still_hint(self):
        t = run(self.tracker, 0, 60, "sitting", talking=True)
        run(self.tracker, t, 60, "lying", body_speed=0.0)
        self.tracker.gap()
        self.assertEqual([s["hint"] for s in self.closed], ["chatting_with_ember", "lying_still"])

    def test_open_segment_is_reported_before_it_closes(self):
        self.assertIsNone(self.tracker.open_segment())
        run(self.tracker, 0, 10, "standing")
        current = self.tracker.open_segment()
        self.assertEqual(current["posture"], "standing")
        self.assertEqual(self.closed, [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "toolkit_activity.sqlite3"

    def segment(self, start, end, hint="sitting", **extra):
        return {"id": f"s{start}", "start": start, "end": end, "posture": hint, "hint": hint,
                "confidence": 1.0, "still_share": 0.5, "movement": 0.02, "hand_to_mouth": 0,
                "talking_s": 0.0, "samples": 10, **extra}

    def test_timeline_clips_to_the_local_day_and_totals_by_hint(self):
        noon = datetime(2026, 11, 12, 12).astimezone().timestamp()
        midnight = datetime(2026, 11, 12).astimezone().timestamp()
        store = Store(self.path)
        store.save(self.segment(midnight - 600, midnight + 600, "lying_still"))
        store.save(self.segment(noon, noon + 1800, "sitting"))
        store.save(self.segment(noon + 1800, noon + 2400, "sitting"), is_open=True)
        store.close()
        day = timeline("2026-11-12", self.path)
        self.assertEqual([s["hint"] for s in day["segments"]], ["lying_still", "sitting", "sitting"])
        self.assertEqual(day["segments"][0]["start"], midnight)
        self.assertEqual(day["totals"], {"lying_still": 600, "sitting": 2400})
        self.assertEqual(day["segments"][2]["open"], 1)

    def test_saving_again_updates_the_same_segment(self):
        store = Store(self.path)
        store.save(self.segment(0, 30), is_open=True)
        store.save(self.segment(0, 90))
        rows = store.between(-1, 1000)
        store.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["end"], rows[0]["open"]), (90, 0))

    def test_recover_closes_segments_left_open_and_prune_removes_old_rows(self):
        store = Store(self.path)
        store.save(self.segment(0, 30), is_open=True)
        store.save(self.segment(10 * 86400, 10 * 86400 + 30))
        store.recover()
        self.assertEqual(store.between(-1, 40)[0]["open"], 0)
        self.assertEqual(store.prune(10 * 86400 + 60, days=5), 1)
        self.assertEqual(len(store.between(-1, 20 * 86400)), 1)
        store.close()

    def test_timeline_never_creates_or_changes_the_store(self):
        self.assertEqual(timeline("2026-11-12", self.path)["segments"], [])
        self.assertFalse(self.path.exists())
        store = Store(self.path)
        store.save(self.segment(0, 30), is_open=True)
        store.close()
        before = Store(self.path).between(-1, 40)
        timeline("1970-01-01", self.path)
        self.assertEqual(Store(self.path).between(-1, 40), before)

    def test_retention_setting(self):
        with patch.dict("os.environ", {"BAYMAX_ACTIVITY_RETENTION_DAYS": "30"}):
            self.assertEqual(store_module.retention_days(), 30)
        with patch.dict("os.environ", {"BAYMAX_ACTIVITY_RETENTION_DAYS": "lots"}):
            self.assertEqual(store_module.retention_days(), 90)


def landmark_objects(points):
    return [SimpleNamespace(x=p[0], y=p[1], z=p[2], visibility=p[3] if len(p) > 3 else 1.0) for p in points]


class FakeStore:
    def __init__(self):
        self.saved, self.closed, self.path = [], False, Path("fake.sqlite3")
    def recover(self): pass
    def prune(self, now, days=None): return 0
    def save(self, segment, is_open=False): self.saved.append((segment, is_open))
    def close(self): self.closed = True


class WorkerTests(unittest.TestCase):
    def test_disabled_never_starts_a_thread(self):
        worker = Activity(store_factory=FakeStore, enabled_check=lambda: False)
        worker.observe(landmark_objects(STANDING[0]))
        self.assertIsNone(worker.thread)
        self.assertEqual(worker.state, "Disabled")

    def test_observer_is_rate_limited_and_feeds_the_tracker(self):
        clock = [1000.0]
        fake = FakeStore()
        worker = Activity(store_factory=lambda: fake, clock=lambda: clock[0], enabled_check=lambda: True)
        worker.observe(landmark_objects(SITTING[0]), landmark_objects(SITTING[1]), (480, 640, 3))
        worker.observe(landmark_objects(SITTING[0]))   # Same instant: dropped.
        deadline = time.time() + 2
        while worker.samples < 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.samples, 1)
        worker.stop()
        self.assertTrue(fake.closed)

    def test_store_failure_turns_tracking_off_without_raising(self):
        def broken():
            raise OSError("disk full")
        worker = Activity(store_factory=broken, enabled_check=lambda: True)
        worker.observe(None)
        worker.thread.join(timeout=2)
        self.assertIn("Unavailable", worker.state)
        worker.observe(None)   # Off now; no new thread, no exception.

    def test_user_speech_counts_as_talking(self):
        worker = Activity(clock=lambda: 50.0, enabled_check=lambda: True)
        worker.heard("ember", {"text": "hello"})
        self.assertIsNone(worker.last_user_speech)
        worker.heard("user", {"text": "  "})
        self.assertIsNone(worker.last_user_speech)
        worker.heard("user", {"text": "I'm having lunch"})
        self.assertEqual(worker.last_user_speech, 50.0)


class HookTests(unittest.TestCase):
    def test_pose_observer_errors_remove_only_that_observer(self):
        seen = []
        def broken(*args):
            raise RuntimeError("boom")
        with patch.object(fall_detection, "pose_observers", [broken, lambda *a: seen.append(a)]):
            fall_detection.notify_pose("lm", "world", (480, 640, 3))
            fall_detection.notify_pose(None)
            self.assertEqual(len(fall_detection.pose_observers), 1)
        self.assertEqual(seen, [("lm", "world", (480, 640, 3)), (None, None, None)])

    def test_toolkit_import_registers_activity_hooks(self):
        import runtime_monitor
        import features.toolkits.gemini_tools  # noqa: F401
        self.assertIn(activity_module.activity.observe, fall_detection.pose_observers)
        self.assertIn(activity_module.activity.heard, runtime_monitor.transcript_listeners)


class V10Tests(unittest.TestCase):
    def function(self, name):
        tree = ast.parse(V10.read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(V10), "exec"), namespace)
        return namespace[name]

    def test_pose_delegate_choice(self):
        delegates = self.function("_pose_delegates")
        self.assertEqual(delegates("auto", "linux", "x86_64"), ["gpu", "cpu"])
        self.assertEqual(delegates("auto", "linux", "aarch64"), ["cpu"])
        self.assertEqual(delegates("auto", "win32", "AMD64"), ["cpu"])
        self.assertEqual(delegates("gpu", "linux", "aarch64"), ["gpu", "cpu"])
        self.assertEqual(delegates("cpu", "linux", "x86_64"), ["cpu"])

    def test_pose_landmarker_falls_back_when_a_delegate_fails(self):
        opened = []
        class Landmarker:
            @staticmethod
            def create_from_options(options):
                if options.base_options.delegate == "GPU":
                    raise RuntimeError("no EGL")
                opened.append(options.base_options.delegate)
                return "landmarker"
        tasks = SimpleNamespace(BaseOptions=lambda **kw: SimpleNamespace(**kw))
        tasks.BaseOptions.Delegate = SimpleNamespace(GPU="GPU", CPU="CPU")
        vision = SimpleNamespace(PoseLandmarker=Landmarker, PoseLandmarkerOptions=lambda **kw: SimpleNamespace(**kw),
                                 RunningMode=SimpleNamespace(VIDEO="VIDEO"))
        tree = ast.parse(V10.read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_open_pose_landmarker")
        namespace = {"mp_tasks": tasks, "mp_vision": vision, "_POSE_MODEL_PATH": "pose.task"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(V10), "exec"), namespace)
        with patch("builtins.print"):
            self.assertEqual(namespace["_open_pose_landmarker"](["gpu", "cpu"]), ("landmarker", "cpu"))
            with self.assertRaises(RuntimeError):
                namespace["_open_pose_landmarker"](["gpu"])
        self.assertEqual(opened, ["CPU"])

    def test_camera_rotation_setting(self):
        tree = ast.parse(V10.read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_camera_rotation")
        cv2 = SimpleNamespace(ROTATE_90_CLOCKWISE=0, ROTATE_90_COUNTERCLOCKWISE=2, ROTATE_180=1)
        namespace = {"cv2": cv2}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(V10), "exec"), namespace)
        rotation = namespace["_camera_rotation"]
        self.assertIsNone(rotation(None))
        self.assertIsNone(rotation("none"))
        self.assertEqual(rotation(" 90CW "), 0)
        self.assertEqual(rotation("90ccw"), 2)
        self.assertEqual(rotation("180"), 1)
        with patch("builtins.print"):
            self.assertIsNone(rotation("sideways"))

    def test_rotated_frames_reach_everyone_and_gemini_keeps_the_shape(self):
        source = V10.read_text(encoding="utf-8")
        loop = source[source.index("def _camera_session"):]
        read = loop.index("ret, frame = cap.read()")
        self.assertLess(loop.index("cv2.rotate(frame, CAMERA_ROTATION)", read), loop.index("_latest_frame = frame", read))
        sender = source[source.index("async def send_video_realtime"):]
        self.assertIn("scale = 320 / max(h, w)", sender[:sender.index("imencode")])

    def test_camera_loop_shares_pose_and_recovers_from_gpu_errors(self):
        source = V10.read_text(encoding="utf-8")
        start = source.index("def _camera_session")
        loop = source[start:source.index("\ndef ", start + 1)]
        self.assertIn("notify_pose(landmarks,", loop)
        self.assertIn('_open_pose_landmarker(["cpu"])', loop)
        self.assertIn("landmarker.close()", loop)


class DevUITests(unittest.TestCase):
    def test_activity_endpoint_validates_the_date_and_reports_the_switch(self):
        import devUI
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("BAYMAX_ACTIVITY_ENABLED=1\n")
            with patch.object(devUI, "ENV", env), \
                    patch.dict("os.environ", {"BAYMAX_DATA_DIR": folder}):
                result = devUI.activity_timeline("2026-11-12")
                self.assertTrue(result["enabled"])
                self.assertEqual(result["segments"], [])
                for bad in ("2026-13-40", "yesterday", 5, "2026-11-12; DROP"):
                    with self.assertRaises(ValueError):
                        devUI.activity_timeline(bad)
            self.assertFalse((Path(folder) / "toolkit_activity.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
