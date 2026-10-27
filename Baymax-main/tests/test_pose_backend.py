"""features/pose: backend choice, MediaPipe-graph math, smoothing and wiring. Offline:
no models, no NPU, no camera. Parity with MediaPipe runs only when converted models,
onnxruntime, mediapipe and the sample image are present, and is skipped otherwise."""
import ast
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from features.pose import backend, models, pipeline as P
from features.pose.engines import split_detector, split_landmarks, heatmap_hwc
import pc_hardware

ROOT = Path(__file__).resolve().parents[1]
TAU = 2 * math.pi


def write_models(directory, passed=True, corrupt=None, drop=None):
    """A model directory with fake .rknn files and a manifest that records their hashes."""
    directory = Path(directory)
    manifest = {"format": models.MANIFEST_FORMAT, "rknn": {}, "onnx": {}, "tools": {"rknn-toolkit2": "2.3.2"}}
    for kind in ("rknn", "onnx"):
        for name in models.NAMES:
            path = directory / f"{name}.{kind}"
            path.write_bytes(f"{kind}-{name}".encode())
            manifest[kind][name] = {"file": path.name, "sha256": models.sha256(path)}
    if passed is not None:
        manifest["check"] = {"passed": passed}
    if corrupt:
        (directory / corrupt).write_bytes(b"changed after conversion")
    if drop:
        (directory / drop).unlink()
    (directory / models.MANIFEST).write_text(json.dumps(manifest))
    return manifest


class NpuSelectionTests(unittest.TestCase):
    """Every gate between auto and the NPU, each with its own reason."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"BAYMAX_POSE_MODEL_DIR": self.temp.name}
        self.rknnlite = Mock()

    def reason(self, forced=False, **overrides):
        probes = dict(system="linux", machine="aarch64", rk3588=lambda: True, rknnlite=self.rknnlite)
        probes.update(overrides)
        return backend.npu_unavailable_reason(self.env, forced=forced, **probes)

    def test_all_gates_pass(self):
        write_models(self.temp.name)
        self.assertIsNone(self.reason())

    def test_other_systems_never_look_for_rknnlite(self):
        for system, machine in (("darwin", "arm64"), ("win32", "AMD64"), ("linux", "x86_64")):
            self.assertIn("not Linux aarch64", self.reason(system=system, machine=machine))
        self.rknnlite.assert_not_called()

    def test_other_arm_boards_are_skipped(self):
        write_models(self.temp.name)
        self.assertIn("not an RK3588 board", self.reason(rk3588=lambda: False))
        self.rknnlite.assert_not_called()

    def test_missing_rknnlite(self):
        def missing():
            raise ImportError("No module named 'rknnlite'")
        self.assertIn("rknn-toolkit-lite2 is not usable", self.reason(rknnlite=missing))

    def test_models_not_converted(self):
        self.assertIn("no converted models", self.reason())

    def test_missing_rknn_file(self):
        write_models(self.temp.name, drop="pose_landmarks_detector.rknn")
        self.assertIn("pose_landmarks_detector.rknn is missing", self.reason())

    def test_checksum_mismatch(self):
        write_models(self.temp.name, corrupt="pose_detector.rknn")
        self.assertIn("does not match the SHA-256", self.reason())

    def test_unknown_manifest_format(self):
        (Path(self.temp.name) / models.MANIFEST).write_text(json.dumps({"format": 99}))
        self.assertIn("unknown format", self.reason())

    def test_accuracy_check_must_have_passed(self):
        write_models(self.temp.name, passed=False)
        self.assertIn("accuracy check", self.reason())
        write_models(self.temp.name, passed=None)
        self.assertIn("accuracy check", self.reason())

    def test_forcing_skips_board_and_accuracy_gates_only(self):
        write_models(self.temp.name, passed=False)
        self.assertIsNone(self.reason(forced=True, rk3588=lambda: False))
        self.assertIn("not Linux aarch64", self.reason(forced=True, system="darwin"))
        write_models(self.temp.name, corrupt="pose_detector.rknn")
        self.assertIn("SHA-256", self.reason(forced=True))


class FakeEngine:
    """Detector finds nothing unless told; landmark model returns a fixed pose."""

    def __init__(self, detection=None, flag=0.9, landmarks=None):
        self.detection = detection
        self.flag = flag
        self.raw = landmarks
        self.detector_calls = 0
        self.landmark_calls = 0
        self.closed = False

    def detector(self, crop):
        assert crop.shape == (224, 224, 3) and crop.dtype == np.uint8
        self.detector_calls += 1
        boxes = np.zeros((2254, 12), np.float32)
        scores = np.full((2254, 1), -10.0, np.float32)
        if self.detection is not None:
            index, raw = self.detection
            boxes[index] = raw
            scores[index] = 5.0
        return boxes, scores

    def landmarks(self, crop):
        assert crop.shape == (256, 256, 3) and crop.dtype == np.uint8
        self.landmark_calls += 1
        raw = self.raw if self.raw is not None else standing_raw()
        return raw, np.array([self.flag], np.float32), np.full((64, 64, 39), -10.0, np.float32)

    def close(self):
        self.closed = True


def standing_raw():
    """39 landmarks in 256-px crop units: an upright figure; aux 33 at the hips, 34 above the head."""
    raw = np.zeros((39, 5), np.float32)
    raw[:, 0], raw[:, 1] = 128, 128
    raw[:, 3], raw[:, 4] = 4.0, 4.0                     # sigmoid -> 0.982
    raw[0, :2] = (128, 60)
    raw[11, :2], raw[12, :2] = (150, 90), (106, 90)
    raw[23, :2], raw[24, :2] = (140, 150), (116, 150)
    raw[33, :2], raw[34, :2] = (128, 150), (128, 50)
    return raw.reshape(-1)


def centred_detection(width=640, height=480):
    """Raw detector output on the anchor nearest the image centre, keypoints 0/1 at the
    hips and above the head, 100 px apart vertically in the 224 input."""
    anchors = P.ssd_anchors()
    index = int(np.argmin(np.hypot(anchors[:, 0] - 0.5, anchors[:, 1] - 0.5)))
    raw = np.zeros(12, np.float32)
    raw[2:4] = 80                                       # w, h in 224 px
    raw[4:6] = (0, 30)                                  # keypoint 0 (x, y) relative to the anchor
    raw[6:8] = (0, -70)                                 # keypoint 1
    return index, raw


class CreateBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = []
        self.cpu = SimpleNamespace(name=backend.CPU_NAME, device="CPU", close=Mock())

    def create(self, env, probe=None, engine_factory=None):
        env = {"BAYMAX_POSE_MODEL_DIR": self.temp.name, **env}
        return backend.create_pose_backend(env=env, log=self.logs.append, cpu_factory=lambda: self.cpu,
                                           probe=probe or (lambda env, forced: None),
                                           engine_factory=engine_factory or (lambda d, l, c: FakeEngine()))

    def test_auto_off_the_rk3588_is_mediapipe_and_says_why(self):
        chosen = self.create({}, probe=lambda env, forced: "not Linux aarch64 (darwin arm64)")
        self.assertIs(chosen, self.cpu)
        self.assertEqual(self.logs, ["Pose backend: CPU (MediaPipe). NPU skipped: not Linux aarch64 (darwin arm64)."])

    def test_cpu_choice_never_probes(self):
        probe = Mock()
        self.assertIs(self.create({"BAYMAX_POSE_BACKEND": "cpu"}, probe=probe), self.cpu)
        probe.assert_not_called()
        self.assertIn("BAYMAX_POSE_BACKEND=cpu", self.logs[0])

    def test_npu_when_every_gate_passes(self):
        write_models(self.temp.name)
        engines = []
        def factory(detector, landmarks, core):
            engines.append((Path(detector).name, Path(landmarks).name, core))
            return FakeEngine()
        chosen = self.create({"BAYMAX_POSE_NPU_CORE": "1"}, engine_factory=factory)
        self.assertEqual((chosen.name, chosen.device), (backend.NPU_NAME, "NPU"))
        self.assertEqual(engines, [("pose_detector.rknn", "pose_landmarks_detector.rknn", "1")])
        self.assertIn("NPU (RK3588) on NPU core 1", self.logs[0])
        self.assertIn("rknn-toolkit2 2.3.2", self.logs[0])
        self.assertIsNone(chosen.detect(np.zeros((480, 640, 3), np.uint8), 1))
        chosen.close()

    def test_default_core_is_zero(self):
        write_models(self.temp.name)
        cores = []
        self.create({}, engine_factory=lambda d, l, c: cores.append(c) or FakeEngine())
        self.assertEqual(cores, ["0"])

    def test_init_runtime_failure_falls_back_with_reason(self):
        write_models(self.temp.name)
        def failing(detector, landmarks, core):
            raise RuntimeError("init_runtime(pose_detector, core 0) returned -1")
        with patch.object(backend, "driver_hint", return_value="kernel 6.12.0; no rknpu driver"):
            self.assertIs(self.create({}, engine_factory=failing), self.cpu)
        self.assertIn("NPU skipped: NPU init failed (RuntimeError: init_runtime(pose_detector, core 0) returned -1); "
                      "kernel 6.12.0; no rknpu driver.", self.logs[0])

    def test_warm_up_failure_closes_the_engine(self):
        write_models(self.temp.name)
        engine = FakeEngine()
        engine.landmarks = Mock(side_effect=RuntimeError("RKNN inference returned nothing"))
        with patch.object(backend, "driver_hint", return_value="kernel x"):
            self.assertIs(self.create({}, engine_factory=lambda d, l, c: engine), self.cpu)
        self.assertTrue(engine.closed)

    def test_forced_npu_passes_forced_to_the_probe(self):
        seen = []
        self.create({"BAYMAX_POSE_BACKEND": "npu"}, probe=lambda env, forced: seen.append(forced) or "x")
        self.assertEqual(seen, [True])

    def test_unknown_choice_means_auto(self):
        self.create({"BAYMAX_POSE_BACKEND": "gpu"}, probe=lambda env, forced: "no")
        self.assertIn("Unknown BAYMAX_POSE_BACKEND='gpu'", self.logs[0])
        self.assertIn("NPU skipped: no", self.logs[1])

    def test_npu_failure_during_use_switches_to_mediapipe(self):
        write_models(self.temp.name)
        engine = FakeEngine()
        cpu = Mock(name="cpu", device="CPU")
        cpu.name = backend.CPU_NAME
        cpu.detect.return_value = "cpu-landmarks"
        chosen = backend.create_pose_backend(env={"BAYMAX_POSE_MODEL_DIR": self.temp.name}, log=self.logs.append,
                                             cpu_factory=lambda: cpu, probe=lambda env, forced: None,
                                             engine_factory=lambda d, l, c: engine)
        engine.detector = Mock(side_effect=RuntimeError("NPU timeout"))
        frame = np.zeros((480, 640, 3), np.uint8)
        self.assertEqual(chosen.detect(frame, 5), "cpu-landmarks")
        self.assertEqual((chosen.name, chosen.device), (backend.CPU_NAME, "CPU"))
        self.assertTrue(engine.closed)
        self.assertIn("switching to CPU (MediaPipe)", self.logs[-1])
        chosen.detect(frame, 6)
        self.assertEqual(cpu.detect.call_count, 2)

    def test_mediapipe_backend_makes_the_runtime_calls(self):
        landmarker = Mock()
        landmarker.detect_for_video.return_value = SimpleNamespace(pose_landmarks=[["lm"]])
        vision = SimpleNamespace(PoseLandmarker=SimpleNamespace(create_from_options=Mock(return_value=landmarker)))
        mp = SimpleNamespace(Image=Mock(return_value="image"), ImageFormat=SimpleNamespace(SRGB="srgb"))
        modules = {"mediapipe": mp, "mediapipe.tasks": SimpleNamespace(), "mediapipe.tasks.python": SimpleNamespace(vision=vision),
                   "mediapipe.tasks.python.vision": vision}
        with patch.dict(sys.modules, modules):
            cpu = backend.MediaPipeCpuBackend("options")
            frame = np.zeros((2, 2, 3), np.uint8)
            frame[0, 0] = (1, 2, 3)
            self.assertEqual(cpu.detect(frame, 42), ["lm"])
        vision.PoseLandmarker.create_from_options.assert_called_once_with("options")
        rgb = mp.Image.call_args.kwargs["data"]
        self.assertEqual(tuple(rgb[0, 0]), (3, 2, 1))   # BGR camera frame -> SRGB, as before
        landmarker.detect_for_video.assert_called_once_with("image", 42)
        landmarker.detect_for_video.return_value = SimpleNamespace(pose_landmarks=[])
        self.assertIsNone(cpu.detect(frame, 43))


class DefaultOffRk3588Tests(unittest.TestCase):
    def test_this_machine_uses_mediapipe_unless_it_is_an_rk3588(self):
        if pc_hardware.is_rk3588():
            self.skipTest("running on an RK3588; see NpuSelectionTests")
        with tempfile.TemporaryDirectory() as temp:
            cpu = object()
            logs = []
            env = {"BAYMAX_POSE_MODEL_DIR": temp}
            chosen = backend.create_pose_backend(env=env, log=logs.append, cpu_factory=lambda: cpu)
        self.assertIs(chosen, cpu)
        self.assertIn("NPU skipped", logs[0])
        self.assertNotIn("rknnlite", sys.modules)

    def test_device_tree_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "compatible"
            path.write_bytes(b"xunlong,orangepi-5-max\0rockchip,rk3588\0")
            self.assertEqual(pc_hardware.device_tree_compatible(path), ("xunlong,orangepi-5-max", "rockchip,rk3588"))
            self.assertTrue(pc_hardware.is_rk3588(pc_hardware.device_tree_compatible(path)))
            self.assertTrue(pc_hardware.is_rk3588(("rockchip,rk3588s",)))
            self.assertFalse(pc_hardware.is_rk3588(("raspberrypi,5-model-b", "brcm,bcm2712")))
            self.assertEqual(pc_hardware.device_tree_compatible(Path(temp) / "missing"), ())
            drivers = Path(temp) / "drivers"
            for name in ("RKNPU", "rockchip-pinctrl"):
                (drivers / name).mkdir(parents=True)
            self.assertEqual(pc_hardware.npu_drivers(str(drivers)), ["RKNPU"])

    def test_rknnlite_is_imported_only_by_the_engine(self):
        offenders = []
        sources = []
        for folder, dirs, files in os.walk(ROOT):
            # Ember's own code only: not .venv*/.models (the converter's venvs), other venvs or caches.
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("venv", "node_modules", "site-packages", "__pycache__")]
            sources += [Path(folder) / f for f in files if f.endswith(".py")]
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                         [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(name.split(".")[0] == "rknnlite" for name in names):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, ["features/pose/engines.py"])
        engine = (ROOT / "features/pose/engines.py").read_text(encoding="utf-8")
        tree = ast.parse(engine)
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any("rknnlite" in ast.dump(n) for n in top_level))   # lazily, inside a function

    def test_importing_the_feature_loads_no_runtime(self):
        code = ("import sys; import features.pose.backend, features.pose.pipeline, features.pose.engines, "
                "features.pose.convert_rknn; "
                "print(sorted(m for m in ('rknnlite', 'rknn', 'onnxruntime', 'mediapipe', 'tensorflow') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


class AnchorAndRoiTests(unittest.TestCase):
    def test_anchor_count_and_layout(self):
        anchors = P.ssd_anchors()
        self.assertEqual(anchors.shape, (2254, 2))
        self.assertTrue(np.allclose(anchors[0], (0.5 / 28, 0.5 / 28)))
        self.assertTrue(np.allclose(anchors[1], anchors[0]))           # two anchors per cell at stride 8
        self.assertTrue(np.allclose(anchors[1568], (0.5 / 14, 0.5 / 14)))
        self.assertTrue(np.allclose(anchors[1960:1966], (0.5 / 7, 0.5 / 7)))   # six at the three stride-32 layers
        self.assertTrue(np.allclose(anchors[-1], (6.5 / 7, 6.5 / 7)))

    def test_letterbox_of_the_camera_frame(self):
        roi, padding = P.detector_roi(640, 480)
        self.assertEqual(roi, (320.0, 240.0, 640.0, 640.0, 0.0))
        self.assertEqual(padding, (0.0, 0.125, 0.0, 0.125))
        roi, padding = P.detector_roi(480, 640)
        self.assertEqual(roi[2:4], (640.0, 640.0))
        self.assertEqual(padding, (0.125, 0.0, 0.125, 0.0))
        detection = P.Detection(0.9, (0.25, 0.125, 0.75, 0.875), [(0.5, 0.125), (0.5, 0.875), (0, 0), (0, 0)])
        clean = P.remove_letterbox(detection, (0.0, 0.125, 0.0, 0.125))
        self.assertTrue(np.allclose(clean.box, (0.25, 0.0, 0.75, 1.0)))
        self.assertTrue(np.allclose(clean.keypoints[:2], [(0.5, 0.0), (0.5, 1.0)]))

    def test_upright_person_roi(self):
        # Hips at the centre, keypoint 1 120 px above them on a 640x480 frame.
        rect = P.alignment_rect((0.5, 0.5), (0.5, 0.25), 640, 480)
        self.assertAlmostEqual(rect.rotation, 0.0)
        self.assertAlmostEqual(rect.width, 240 / 640)
        self.assertAlmostEqual(rect.height, 240 / 480)
        roi = P.transform_rect(rect, 640, 480)
        self.assertAlmostEqual(roi.width * 640, 300.0)                # x1.25, square in pixels
        self.assertAlmostEqual(roi.height * 480, 300.0)
        self.assertEqual((roi.x_center, roi.y_center), (0.5, 0.5))

    def test_lying_person_roi_is_rotated(self):
        rect = P.alignment_rect((0.5, 0.5), (0.75, 0.5), 640, 480)     # head to the right
        self.assertAlmostEqual(rect.rotation, math.pi / 2)
        rect = P.alignment_rect((0.5, 0.5), (0.25, 0.5), 640, 480)     # head to the left
        self.assertAlmostEqual(rect.rotation, -math.pi / 2)
        rect = P.alignment_rect((0.5, 0.5), (0.5, 0.75), 640, 480)     # upside down
        self.assertAlmostEqual(abs(rect.rotation), math.pi)
        for angle in (-7.0, -math.pi, 0.0, 3.5, 12.0):
            value = P.normalize_radians(angle)
            self.assertTrue(-math.pi <= value < math.pi)
            self.assertAlmostEqual(math.cos(value), math.cos(angle))

    def test_decode_weighted_nms_and_association(self):
        anchors = P.ssd_anchors()
        boxes = np.zeros((2254, 12), np.float32)
        scores = np.full((2254, 1), -10.0, np.float32)
        a, b, c = 300, 301, 1000                                       # a/b share a cell; c is elsewhere
        boxes[a, 2:4] = boxes[b, 2:4] = boxes[c, 2:4] = 40
        boxes[b, 0] = 2.0                                              # b shifted 2 px right
        boxes[a, 4:6] = (0, 10)
        boxes[b, 4:6] = (2, 10)
        scores[a], scores[b], scores[c] = 2.0, 0.0, 1.0
        scores[5] = -0.1                                               # sigmoid < 0.5: dropped
        detections = P.decode_detections(boxes, scores, anchors, 0.5)
        self.assertEqual(len(detections), 3)
        self.assertAlmostEqual(detections[0].box[2] - detections[0].box[0], 40 / 224, places=6)
        merged = P.weighted_nms(detections)
        self.assertEqual(len(merged), 2)
        wa, wb = 1 / (1 + math.exp(-2.0)), 0.5
        expected_x = anchors[a, 0] + (wb * 2.0 / 224) / (wa + wb)
        self.assertAlmostEqual(merged[0].keypoints[0, 0], expected_x, places=6)
        self.assertAlmostEqual(merged[0].score, wa, places=6)          # the top score is kept
        self.assertAlmostEqual(merged[1].keypoints[0, 0], anchors[c, 0], places=6)
        first = P.Rect(0.5, 0.5, 0.2, 0.2)
        overlapping = P.Rect(0.51, 0.5, 0.2, 0.2)
        apart = P.Rect(0.1, 0.1, 0.1, 0.1)
        self.assertEqual(P.associate([first, apart, overlapping], 0.5), [apart, overlapping])


class LandmarkMathTests(unittest.TestCase):
    def test_decode_uses_crop_pixels_and_sigmoid(self):
        raw = np.zeros((39, 5), np.float32)
        raw[0] = (128, 64, 25.6, 0.0, 2.0)
        out = P.decode_landmarks(raw.reshape(-1))
        self.assertTrue(np.allclose(out[0], (0.5, 0.25, 0.1, 0.5, 1 / (1 + math.exp(-2)))))

    def test_projection_onto_a_rotated_roi(self):
        landmarks = np.array([[0.5, 0.5, 0.1, 1, 1], [1.0, 0.5, 0.0, 1, 1], [0.5, 0.0, 0.0, 1, 1]])
        rect = P.Rect(0.4, 0.6, 0.5, 0.25, 0.0)
        out = P.project(landmarks, rect)
        self.assertTrue(np.allclose(out[0, :3], (0.4, 0.6, 0.05)))      # centre; z scales like x
        self.assertTrue(np.allclose(out[1, :2], (0.65, 0.6)))
        out = P.project(landmarks, P.Rect(0.4, 0.6, 0.5, 0.25, math.pi / 2))
        self.assertTrue(np.allclose(out[1, :2], (0.4, 0.725)))          # crop's right edge points down
        self.assertTrue(np.allclose(out[2, :2], (0.65, 0.6)))           # crop's top points right

    def test_heatmap_refinement(self):
        landmarks = np.zeros((39, 5))
        landmarks[:, :2] = 0.5
        landmarks[1, :2] = (-0.2, 0.5)                                  # outside: untouched
        heatmap = np.full((64, 64, 39), -10.0, np.float32)
        heatmap[30, 34, 0] = 10.0                                       # one confident cell near (32, 32)
        heatmap[:, :, 2] = 0.0                                          # sigmoid 0.5 everywhere: refined to the window centre
        heatmap[:, :, 3] = -1.0                                         # sigmoid 0.27 < 0.5: not refined
        out = P.refine_from_heatmap(landmarks.copy(), heatmap)
        self.assertAlmostEqual(out[0, 0], 34 / 64, places=3)
        self.assertAlmostEqual(out[0, 1], 30 / 64, places=3)
        self.assertTrue(np.allclose(out[1, :2], (-0.2, 0.5)))
        self.assertTrue(np.allclose(out[2, :2], (32 / 64, 32 / 64)))
        self.assertTrue(np.allclose(out[3, :2], (0.5, 0.5)))

    def test_engine_outputs_by_size_and_layout(self):
        boxes, scores = split_detector([np.zeros((1, 2254, 1)), np.ones((1, 2254, 12))])
        self.assertEqual((boxes.shape, scores.shape, boxes[0, 0], scores[0, 0]), ((2254, 12), (2254, 1), 1.0, 0.0))
        nhwc = np.arange(64 * 64 * 39, dtype=np.float32).reshape(1, 64, 64, 39)
        self.assertTrue(np.array_equal(heatmap_hwc(nhwc.transpose(0, 3, 1, 2)), nhwc[0]))
        raw, flag, heatmap = split_landmarks([nhwc, np.full((1, 1), 0.7), np.zeros((1, 195))])
        self.assertEqual((raw.shape, float(flag[0]), heatmap.shape), ((195,), np.float32(0.7), (64, 64, 39)))
        with self.assertRaises(RuntimeError):
            split_landmarks([np.zeros((1, 195))])


class SmoothingTests(unittest.TestCase):
    def test_one_euro_matches_mediapipe_rules(self):
        f = P.OneEuro(min_cutoff=0.05, beta=80.0, derivate_cutoff=1.0)
        self.assertEqual(float(f.apply(1_000_000, np.array([10.0]), 1.0)[0]), 10.0)      # first value passes
        self.assertEqual(float(f.apply(1_000_000, np.array([99.0]), 1.0)[0]), 99.0)      # same timestamp: raw
        still = f.apply(1_066_000_000, np.array([10.0]), 1.0)
        self.assertAlmostEqual(float(still[0]), 10.0)
        jump = float(f.apply(1_132_000_000, np.array([11.0]), 0.01)[0])
        self.assertTrue(10.0 < jump < 11.0)                                              # a small move is smoothed
        fast = P.OneEuro(0.05, 80.0, 1.0)
        fast.apply(1_000_000, np.array([10.0]), 1.0)
        self.assertGreater(float(fast.apply(67_000_000, np.array([11.0]), 1.0)[0]), jump)  # a big scaled move follows faster

    def test_visibility_survives_lost_tracking_but_positions_restart(self):
        smoother = P.LandmarkSmoother()
        rect = P.Rect(0.5, 0.5, 0.5, 0.5)
        pose = np.zeros((33, 5))
        pose[:, :2] = 0.5
        pose[:, 3] = 1.0
        smoother.apply(pose, 1000, 640, 480, rect)
        smoother.reset_position()                       # a frame without a pose
        moved = pose.copy()
        moved[:, 0] = 0.6
        moved[:, 3] = 0.0
        out = smoother.apply(moved, 200_000, 640, 480, rect)
        self.assertTrue(np.allclose(out[:, 0], 0.6))    # positions start over
        self.assertTrue(np.allclose(out[:, 3], 0.9))    # visibility: 0.1 * 0 + 0.9 * 1

    def test_tiny_roi_is_not_filtered(self):
        smoother = P.LandmarkSmoother()
        pose = np.full((33, 5), 0.5)
        out = smoother.apply(pose, 1000, 640, 480, P.Rect(0.5, 0.5, 0.0, 0.0))
        self.assertTrue(np.allclose(out, pose))


class PipelineFlowTests(unittest.TestCase):
    frame = np.full((480, 640, 3), 90, np.uint8)

    def test_detect_then_track_then_lose(self):
        engine = FakeEngine(detection=centred_detection())
        pose = P.PosePipeline(engine)
        first = pose.detect(self.frame, 10)
        self.assertEqual(len(first), 33)
        self.assertTrue(pose.detector_ran)
        landmark = first[0]
        for attribute in ("x", "y", "z", "visibility", "presence"):
            self.assertIsInstance(getattr(landmark, attribute), float)
        self.assertAlmostEqual(landmark.visibility, 1 / (1 + math.exp(-4.0)), places=5)
        # Upright: the nose is above the hips, shoulders above hips.
        self.assertLess(first[0].y, first[23].y)
        self.assertLess(first[11].y, first[23].y)
        self.assertEqual(engine.detector_calls, 1)
        second = pose.detect(self.frame, 76)
        self.assertFalse(pose.detector_ran)              # tracked from the auxiliary landmarks
        self.assertEqual(engine.detector_calls, 1)
        self.assertAlmostEqual(second[11].x, first[11].x, delta=0.01)   # new ROI, same person
        engine.flag = 0.4                                # pose flag below 0.5: lost
        self.assertIsNone(pose.detect(self.frame, 142))
        engine.flag = 0.9
        pose.detect(self.frame, 208)
        self.assertEqual(engine.detector_calls, 2)       # re-detected on the next frame

    def test_nothing_found(self):
        engine = FakeEngine()
        pose = P.PosePipeline(engine)
        self.assertIsNone(pose.detect(self.frame, 1))
        self.assertIsNone(pose.detect(self.frame, 2))
        self.assertEqual((engine.detector_calls, engine.landmark_calls), (2, 0))

    def test_timestamps_must_increase(self):
        pose = P.PosePipeline(FakeEngine())
        pose.detect(self.frame, 5)
        with self.assertRaises(ValueError):
            pose.detect(self.frame, 5)

    def test_tracking_roi_follows_the_auxiliary_landmarks(self):
        engine = FakeEngine(detection=centred_detection())
        pose = P.PosePipeline(engine)
        pose.detect(self.frame, 1)
        upright = pose.tracking_rect
        self.assertIsNotNone(upright)
        self.assertAlmostEqual(upright.rotation, 0.0, places=5)
        lying = standing_raw().reshape(39, 5).copy()
        lying[34, :2] = (228, 150)                        # keypoint above the head now to the right
        engine.raw = lying.reshape(-1)
        pose.detect(self.frame, 70)
        self.assertAlmostEqual(abs(pose.tracking_rect.rotation), math.pi / 2, delta=0.3)


class RuntimeWiringTests(unittest.TestCase):
    def test_camera_thread_uses_the_backend_with_its_mediapipe_options(self):
        source = (ROOT / "realtime_gemini_10.py").read_text(encoding="utf-8")
        start = source.index("def _fall_detection_thread")
        loop = source[start:source.index("\n# ───", start)]
        self.assertIn("create_pose_backend(mediapipe_options=options)", loop)
        self.assertIn("landmarks = landmarker.detect(frame, timestamp_ms)", loop)
        self.assertIn("device=landmarker.device", loop)
        for setting in ("running_mode=mp_vision.RunningMode.VIDEO", "num_poses=1", "min_pose_detection_confidence=0.5",
                        "min_pose_presence_confidence=0.5", "min_tracking_confidence=0.5",
                        "mp_tasks.BaseOptions(model_asset_path=_POSE_MODEL_PATH)"):
            self.assertIn(setting, loop)

    def test_converter_without_its_environments_explains_the_setup(self):
        from features.pose import convert_rknn
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"BAYMAX_POSE_MODEL_DIR": temp}), \
                patch("builtins.print") as printed:
            self.assertEqual(convert_rknn.main(["all"]), 2)
        self.assertIn("setup_converter.sh", printed.call_args.args[0])

    def test_converter_rejects_a_different_task(self):
        from features.pose import convert_rknn
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "other.task"
            with zipfile.ZipFile(task, "w") as bundle:
                for name in models.NAMES:
                    bundle.writestr(name + ".tflite", b"not the pinned model")
            with self.assertRaises(RuntimeError):
                convert_rknn.read_task(task)


class ParityWithMediaPipeTests(unittest.TestCase):
    """Needs converted ONNX models, onnxruntime, mediapipe, the .task and the converter's
    cached sample image; skipped when any is missing (e.g. in CI or on a fresh checkout)."""

    def test_port_matches_mediapipe_on_the_sample_image(self):
        directory = models.model_dir()
        image_path = directory / "build" / "pose.jpg"
        tasks = [ROOT / "pose_landmarker_full.task", ROOT / ".models" / "pose_landmarker_full.task",
                 directory / "build" / "pose_landmarker_full.task"]
        task = next((t for t in tasks if t.is_file()), tasks[0])
        try:
            import onnxruntime  # noqa: F401
            import mediapipe  # noqa: F401
            import cv2
            paths = models.verified_files(directory, models.load_manifest(directory), "onnx")
        except (ImportError, models.ModelError) as error:
            self.skipTest(f"parity needs converted models and runtimes: {error}")
        if not image_path.is_file() or not task.is_file():
            self.skipTest("parity needs the converter's sample image and the runtime's .task")
        from features.pose.engines import OnnxEngine
        image = cv2.imread(str(image_path))
        cpu = backend.MediaPipeCpuBackend(backend.default_mediapipe_options(task))
        port = backend.PipelineBackend(OnnxEngine(paths["pose_detector"], paths["pose_landmarks_detector"]), "port", "CPU")
        try:
            for timestamp in (1, 67, 133):
                a, b = cpu.detect(image, timestamp), port.detect(image, timestamp)
                self.assertIsNotNone(a)
                self.assertIsNotNone(b)
                error = [math.hypot(p.x - q.x, p.y - q.y) for p, q in zip(a, b)]
                self.assertLess(float(np.median(error)), 0.003)
                self.assertLess(max(error), 0.02)
                self.assertEqual([p.visibility >= 0.5 for p in a], [q.visibility >= 0.5 for q in b])
        finally:
            cpu.close()
            port.close()


if __name__ == "__main__":
    unittest.main()
