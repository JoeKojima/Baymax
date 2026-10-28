"""Convert Ember's pose models for the RK3588 NPU, on the Orange Pi itself.

    features/pose/setup_converter.sh          # once: two private venvs under the build dir
    .venv/bin/python features/pose/convert_rknn.py          # = all: export-onnx, build-rknn, check

Steps (each runs in the Python that has its tools; `all` calls them in turn):

export-onnx  (venv-onnx: tensorflow, tf2onnx, onnx, onnxruntime, ai-edge-litert)
    Takes pose_landmarker_full.task (the runtime's copy, or downloads version 1),
    checks the SHA-256 of both TFLite files inside, densifies the detector's sparse
    weights, converts both with tf2onnx (opset 13, NCHW input), keeps only the outputs
    the pipeline uses, and compares every ONNX output with the TFLite model on the
    same inputs. Writes the .onnx files and the manifest's source/onnx sections.
build-rknn   (venv-rknn: rknn-toolkit2 2.3.2 or 2.5.0)
    FP16 (do_quantization=False) RKNN models for target rk3588 with MediaPipe's input
    normalisation baked in (uint8 NHWC input). Records SHA-256 and toolkit version.
check        (venv-rknn, which also has rknn-toolkit-lite2, on the board's NPU)
    Runs the RKNN models on the NPU and the ONNX models on ONNX Runtime with the same
    inputs: raw outputs on identical crops (landmarks 0-34 x/y, including the two that
    steer tracking, visibility of 0-32 and the pose flag), then the whole pose pipeline,
    each engine tracking on its own, over still frames and a synthetic tracked sequence
    (sway, person leaves and returns, falls over and lies still), plus --video if given:
    landmark x/y and which landmarks are visible (>= 0.5, what FallDetector and face
    presence use). The runtime uses the NPU automatically only after this check passed.
    --video makes the clip part of that gate, whose limits are set for the synthetic
    sequence: real clips usually miss them, and a failed check keeps Ember on the CPU
    (a plain `check` restores the NPU). To judge the NPU on real footage, use
    `python -m features.pose.validate --engine npu` instead.

Everything is written to the model directory (default Baymax-main/.models/pose_npu,
BAYMAX_POSE_MODEL_DIR overrides). Nothing is uploaded; downloads are the .task and a
MediaPipe sample image, both checked against SHA-256s recorded below.
"""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # Baymax-main
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.pose import models  # noqa: E402

OPSET = 13
KEEP_OUTPUTS = {"pose_detector": ["Identity", "Identity_1"],
                "pose_landmarks_detector": ["Identity", "Identity_1", "Identity_3"]}  # landmarks, flag, heatmap
NORMALIZATION = {"pose_detector": (127.5, 127.5), "pose_landmarks_detector": (0.0, 255.0)}
INPUT_SIZE = {"pose_detector": 224, "pose_landmarks_detector": 256}
SAMPLE_URL = "https://storage.googleapis.com/mediapipe-assets/pose.jpg"   # MediaPipe test image
SAMPLE_SHA256 = "c8a830ed683c0276d713dd5aeda28f415f10cd6291972084a40d0d8b934ed62b"
ONNX_TOLERANCE = 1e-3          # max |onnx - tflite| <= tolerance * max(1, max |tflite|)
# NPU vs ONNX acceptance (FP16 on the NPU): raw landmark x/y (0-34) in 256-px crop units,
# raw visibility of 0-32 and the pose flag (probabilities), and the whole pipeline in
# normalised image coordinates plus the share of landmarks on the same side of visibility
# 0.5 (FallDetector skips a frame when 11/12/23/24 is below it; face presence needs 0/2/5
# at or above it). On the validation clips MediaPipe itself moves by p95 0.005-0.01 when
# +-1 grey level of noise is added (VALIDATION.md). The minimum counts stop a check that
# compared (almost) nothing from passing: crops = landmark-model inputs compared raw; pose
# frames = frames where both pipelines found a pose; tracked frames = those where both
# were tracking from the previous frame's landmarks 33/34 rather than running the detector.
CHECK_RAW_XY_MAX = 3.0
CHECK_RAW_VISIBILITY_MAX = 0.05
CHECK_POSE_FLAG_MAX = 0.05
CHECK_PIPELINE_P95 = 0.01
CHECK_PIPELINE_P99 = 0.03
CHECK_DETECTION_AGREEMENT = 0.98
CHECK_VISIBILITY_AGREEMENT = 0.98
CHECK_MIN_CROPS = 10
CHECK_MIN_POSE_FRAMES = 50
CHECK_MIN_TRACKED_FRAMES = 30
SEQUENCE_FRAMES = 72           # the synthetic tracked sequence, 15 fps like the runtime
FRAME_MS = 66
KEY_LANDMARKS = (0, 2, 5, 11, 12, 23, 24)   # face presence (0, 2, 5) and FallDetector (11, 12, 23, 24)


def log(message):
    print(f"[POSE-CONVERT] {message}", flush=True)


def build_dir(directory):
    return Path(directory) / "build"


def version_of(package):
    try:
        from importlib.metadata import version
        return version(package)
    except Exception:
        return None


def download(url, expected_sha256, max_bytes, destination):
    destination = Path(destination)
    if destination.is_file() and models.sha256(destination) == expected_sha256:
        return destination
    log(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(f"{url} is larger than expected")
    import hashlib
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise RuntimeError(f"{url} failed its SHA-256 check")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination


def sample_image(directory):
    return download(SAMPLE_URL, SAMPLE_SHA256, 1 << 20, build_dir(directory) / "pose.jpg")


# ── export-onnx ──────────────────────────────────────────────────────────────
def find_task(explicit, directory):
    """A pose_landmarker_full.task whose two TFLite files have the pinned hashes."""
    candidates = [Path(explicit)] if explicit else [
        ROOT / "pose_landmarker_full.task", ROOT / ".models" / "pose_landmarker_full.task"]
    for candidate in candidates:
        if candidate.is_file():
            try:
                tflites = read_task(candidate)
            except (RuntimeError, KeyError, zipfile.BadZipFile, OSError) as error:
                if explicit:                   # other weights, renamed members, a truncated file
                    raise
                log(f"Ignoring {candidate}: {type(error).__name__}: {error}")
                continue
            return candidate, tflites
    path = download(models.TASK_URL, models.TASK_SHA256, models.TASK_MAX_BYTES,
                    build_dir(directory) / "pose_landmarker_full.task")
    return path, read_task(path)


def read_task(path):
    import hashlib
    with zipfile.ZipFile(path) as bundle:
        tflites = {}
        for name in models.NAMES:
            data = bundle.read(name + ".tflite")
            if hashlib.sha256(data).hexdigest() != models.TFLITE_SHA256[name]:
                raise RuntimeError(f"{name}.tflite in {path} is not the pinned model")
            tflites[name] = data
    return tflites


def tflite_interpreter(model_path, preserve=False):
    """LiteRT if installed, else TensorFlow's interpreter; without delegates when preserving tensors."""
    try:
        from ai_edge_litert.interpreter import Interpreter, OpResolverType
    except ImportError:
        import tensorflow as tf
        Interpreter, OpResolverType = tf.lite.Interpreter, tf.lite.experimental.OpResolverType
    kwargs = {}
    if preserve:
        kwargs = dict(experimental_preserve_all_tensors=True,
                      experimental_op_resolver_type=OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES)
    interpreter = Interpreter(model_path=str(model_path), **kwargs)
    interpreter.allocate_tensors()
    return interpreter


def densify(source, destination):
    """Replace DENSIFY ops (sparse constant weights) by their dense results: tf2onnx can't
    read sparse tensors. Returns how many were replaced."""
    import numpy as np
    from tensorflow.lite.python import schema_py_generated as schema
    from tensorflow.lite.tools import flatbuffer_utils
    interpreter = tflite_interpreter(source, preserve=True)
    detail = interpreter.get_input_details()[0]
    interpreter.set_tensor(detail["index"], np.zeros(detail["shape"], detail["dtype"]))
    interpreter.invoke()
    model = flatbuffer_utils.read_model(str(source))
    codes = [max(code.builtinCode, code.deprecatedBuiltinCode) for code in model.operatorCodes]
    replaced = 0
    for graph in model.subgraphs:
        kept = []
        for op in graph.operators:
            if codes[op.opcodeIndex] != schema.BuiltinOperator.DENSIFY:
                kept.append(op)
                continue
            sparse, dense = op.inputs[0], op.outputs[0]
            buffer = schema.BufferT()
            buffer.data = np.frombuffer(interpreter.get_tensor(dense).tobytes(), np.uint8)
            model.buffers.append(buffer)
            graph.tensors[dense].buffer = len(model.buffers) - 1
            graph.tensors[sparse].buffer = 0
            graph.tensors[sparse].sparsity = None
            replaced += 1
        graph.operators = kept
    flatbuffer_utils.write_model(model, str(destination))
    return replaced


def compare_onnx_tflite(name, tflite_path, onnx_path, images):
    """Max |ONNX - TFLite| per kept output over random and real inputs."""
    import numpy as np
    import onnxruntime as ort
    mean, std = NORMALIZATION[name]
    size = INPUT_SIZE[name]
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    interpreter = tflite_interpreter(tflite_path)
    out_details = {d["name"]: d["index"] for d in interpreter.get_output_details()}
    rng = np.random.default_rng(0)
    inputs = [rng.integers(0, 256, (size, size, 3), dtype=np.uint8) for _ in range(2)]
    import cv2
    inputs += [cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)[:, :, ::-1] for image in images]
    worst = {}
    for crop in inputs:
        x = crop.astype(np.float32)[None] * np.float32(1 / std) + np.float32(-mean / std)
        interpreter.set_tensor(interpreter.get_input_details()[0]["index"], x)
        interpreter.invoke()
        got = dict(zip([o.name for o in session.get_outputs()],
                       session.run(None, {session.get_inputs()[0].name: np.ascontiguousarray(x.transpose(0, 3, 1, 2))})))
        for output in KEEP_OUTPUTS[name]:
            reference = interpreter.get_tensor(out_details[output])
            diff = float(np.abs(got[output].reshape(reference.shape) - reference).max())
            scale = max(1.0, float(np.abs(reference).max()))
            previous = worst.get(output, (0.0, 1.0))
            if diff / scale >= previous[0] / previous[1]:
                worst[output] = (diff, scale)
    return {output: {"max_abs_diff": round(d, 6), "max_abs_ref": round(s, 3)} for output, (d, s) in worst.items()}


def export_onnx(args):
    import onnx
    import tf2onnx
    directory = models.model_dir()
    work = build_dir(directory) / "onnx"
    work.mkdir(parents=True, exist_ok=True)
    task_path, tflites = find_task(args.task, directory)
    log(f"Using {task_path} (TFLite SHA-256s match the pinned models).")
    import cv2
    images = [cv2.imread(str(sample_image(directory)))]
    manifest = {"format": models.MANIFEST_FORMAT, "source": {
        "task": task_path.name, "task_url": models.TASK_URL, "tflite_sha256": dict(models.TFLITE_SHA256)},
        "onnx": {}}
    for name in models.NAMES:
        original = work / f"{name}.tflite"
        original.write_bytes(tflites[name])
        convertible = work / f"{name}.dense.tflite"
        count = densify(original, convertible)
        log(f"{name}: {count} sparse weight tensors densified." if count else f"{name}: no sparse weights.")
        full = work / f"{name}.full.onnx"
        tf2onnx.convert.from_tflite(str(convertible), inputs_as_nchw=["input_1"], opset=OPSET, output_path=str(full))
        final = Path(directory) / f"{name}.onnx"
        temporary = work / f"{name}.onnx"
        onnx.utils.extract_model(str(full), str(temporary), ["input_1"], KEEP_OUTPUTS[name])
        diffs = compare_onnx_tflite(name, original, temporary, images)
        for output, values in diffs.items():
            if values["max_abs_diff"] > ONNX_TOLERANCE * values["max_abs_ref"]:
                raise RuntimeError(f"{name} {output}: ONNX differs from TFLite by {values['max_abs_diff']} "
                                   f"(max |ref| {values['max_abs_ref']}); not using it")
        shutil.copyfile(temporary, final)
        mean, std = NORMALIZATION[name]
        manifest["onnx"][name] = {
            "file": final.name, "sha256": models.sha256(final), "input": "input_1",
            "input_shape": [1, 3, INPUT_SIZE[name], INPUT_SIZE[name]], "input_mean": mean, "input_std": std,
            "outputs": KEEP_OUTPUTS[name], "opset": OPSET, "vs_tflite": diffs}
        log(f"{name}: ONNX written, matches TFLite (worst {max(v['max_abs_diff'] for v in diffs.values())}).")
    manifest["tools"] = {"python": platform.python_version(), "machine": platform.machine(),
                         "tensorflow": version_of("tensorflow"), "tf2onnx": version_of("tf2onnx"),
                         "onnx": version_of("onnx"), "onnxruntime": version_of("onnxruntime")}
    manifest["created"] = _now()
    models.write_manifest(directory, manifest)        # replaces any older rknn/check sections
    log(f"ONNX models and manifest written to {directory}.")


# ── build-rknn ───────────────────────────────────────────────────────────────
def build_rknn(args):
    from rknn.api import RKNN
    directory = models.model_dir()
    manifest = models.load_manifest(directory)
    onnx_paths = models.verified_files(directory, manifest, "onnx")
    toolkit = version_of("rknn-toolkit2") or "unknown"
    log(f"rknn-toolkit2 {toolkit}, target {args.target}, FP16.")
    manifest["rknn"] = {}
    for name in models.NAMES:
        mean, std = NORMALIZATION[name]
        output = Path(directory) / f"{name}.rknn"
        rknn = RKNN(verbose=args.verbose)
        try:
            rknn.config(mean_values=[[mean] * 3], std_values=[[std] * 3], target_platform=args.target)
            _ok(rknn.load_onnx(model=str(onnx_paths[name])), f"load_onnx({name})")
            _ok(rknn.build(do_quantization=False), f"build({name})")
            temporary = build_dir(directory) / f"{name}.rknn"
            _ok(rknn.export_rknn(str(temporary)), f"export_rknn({name})")
        finally:
            rknn.release()
        shutil.copyfile(temporary, output)
        manifest["rknn"][name] = {"file": output.name, "sha256": models.sha256(output), "target": args.target,
                                  "dtype": "float16", "input": "uint8 NHWC RGB", "mean": mean, "std": std,
                                  "from_onnx_sha256": manifest["onnx"][name]["sha256"]}
        log(f"{name}: {output} written.")
    manifest.setdefault("tools", {}).update({"rknn-toolkit2": toolkit, "rknn_python": platform.python_version(),
                                             "rknn_machine": platform.machine()})
    manifest.pop("check", None)                       # a new model needs a new check
    manifest["converted"] = _now()
    models.write_manifest(directory, manifest)


def _ok(ret, what):
    if ret != 0:
        raise RuntimeError(f"{what} returned {ret}")


# ── check ──────────────────────────────────────────────────────────────────
def tracked_sequence(image, count=SEQUENCE_FRAMES):
    """A 640x480 sequence for checking tracking: the image letterboxed on grey, swaying
    left and right; the person leaves for 3 frames (re-detection; visibility smoothing
    carries over); returns; falls over in 12 frames (0.8 s) about the bottom centre and
    lies still. Rotation exercises rotated ROIs, the case fall detection depends on."""
    import cv2
    import numpy as np
    h, w = image.shape[:2]
    scale = min(640 / w, 480 / h)
    size = (int(round(w * scale)), int(round(h * scale)))
    base = np.full((480, 640, 3), 40, np.uint8)
    x0, y0 = (640 - size[0]) // 2, (480 - size[1]) // 2
    base[y0:y0 + size[1], x0:x0 + size[0]] = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    pivot = (320.0, y0 + size[1] - 0.05 * size[1])
    gone = range(count // 3, count // 3 + 3)
    fall_start, fall_frames = (2 * count) // 3, 12
    frames = []
    for i in range(count):
        if i in gone:
            frames.append(np.full((480, 640, 3), 40, np.uint8))
            continue
        angle = 90.0 * min(max(i - fall_start, 0), fall_frames) / fall_frames
        matrix = cv2.getRotationMatrix2D(pivot, angle, 1.0)
        matrix[0, 2] += 12.0 * math.sin(i / 6.0)                 # sway
        frames.append(cv2.warpAffine(base, matrix, (640, 480), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=(40, 40, 40)))
    return frames


def check_sequences(image_paths, video=None, max_frames=300, count=SEQUENCE_FRAMES):
    """[(kind, frames)]: each still (as is, mirrored, darker) on its own; the synthetic
    tracked sequence from the first image; --video frames as one sequence."""
    import cv2
    sequences = []
    for index, path in enumerate(image_paths):
        image = cv2.imread(str(path))
        if image is None:
            raise RuntimeError(f"cannot read {path}")
        for frame in (image, cv2.flip(image, 1), cv2.convertScaleAbs(image, alpha=0.6, beta=0)):
            sequences.append(("still", [frame]))
        if index == 0 and count:
            sequences.append(("tracked", tracked_sequence(image, count)))
    if video:
        capture = cv2.VideoCapture(str(video))
        frames = []
        while len(frames) < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
        capture.release()
        if not frames:
            raise RuntimeError(f"no frames in {video}")
        sequences.append(("video", frames))
    return sequences


def compare_raw(cpu, npu, frames, rng):
    """Max differences of the raw outputs on identical crops (ONNX pipeline's ROIs).
    Visibility and presence are compared as probabilities (after the sigmoid), x/y/z in
    256-px crop units."""
    import cv2
    import numpy as np
    from features.pose.pipeline import PosePipeline, warp, decode_landmarks, LANDMARK_SIZE
    raw = {"detector_box_max": 0.0, "detector_score_max": 0.0, "landmark_xy_max": 0.0,
           "aux_landmark_xy_max": 0.0, "visibility_max": 0.0, "presence_max": 0.0, "landmark_z_max": 0.0,
           "pose_flag_max": 0.0, "heatmap_max": 0.0, "crops": 0}
    finder = PosePipeline(cpu)
    for frame in list(frames) + [rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)]:
        crop = cv2.resize(frame, (224, 224))[:, :, ::-1].copy()
        (b1, s1), (b2, s2) = cpu.detector(crop), npu.detector(crop)
        confident = s1[:, 0] > -2.0               # boxes near the detection threshold matter most
        if confident.any():
            raw["detector_box_max"] = max(raw["detector_box_max"], float(np.abs(b1 - b2)[confident].max()))
            raw["detector_score_max"] = max(raw["detector_score_max"], float(np.abs(s1 - s2)[confident].max()))
        rect = finder.detect_rect(frame)
        if rect is None:
            continue
        h, w = frame.shape[:2]
        crop = warp(frame, (rect.x_center * w, rect.y_center * h, rect.width * w, rect.height * h, rect.rotation),
                    LANDMARK_SIZE, cv2.BORDER_REPLICATE)[:, :, ::-1].copy()
        (r1, f1, h1), (r2, f2, h2) = cpu.landmarks(crop), npu.landmarks(crop)
        xy = np.abs(r1.reshape(39, 5)[:35, :2] - r2.reshape(39, 5)[:35, :2])   # 33 pose + 2 tracking
        raw["landmark_xy_max"] = max(raw["landmark_xy_max"], float(xy.max()))
        raw["aux_landmark_xy_max"] = max(raw["aux_landmark_xy_max"], float(xy[33:].max()))
        d1, d2 = decode_landmarks(r1)[:33], decode_landmarks(r2)[:33]           # sigmoid visibility/presence
        raw["visibility_max"] = max(raw["visibility_max"], float(np.abs(d1[:, 3] - d2[:, 3]).max()))
        raw["presence_max"] = max(raw["presence_max"], float(np.abs(d1[:, 4] - d2[:, 4]).max()))
        raw["landmark_z_max"] = max(raw["landmark_z_max"], float(np.abs(d1[:, 2] - d2[:, 2]).max() * LANDMARK_SIZE))
        raw["pose_flag_max"] = max(raw["pose_flag_max"], float(abs(f1[0] - f2[0])))
        raw["heatmap_max"] = max(raw["heatmap_max"], float(np.abs(h1 - h2).max()))
        raw["crops"] += 1
    return raw


def compare_pipelines(cpu, npu, sequences):
    """Both engines through the whole pipeline, each with its own tracker per sequence."""
    import time
    import numpy as np
    from features.pose.pipeline import PosePipeline
    errors, timings = [], {"onnx": [], "npu": []}
    counts = {"frames": 0, "agree": 0, "pose_frames": 0, "tracked_frames": 0, "vis_agree": 0, "vis_total": 0}
    key_agree = np.zeros(len(KEY_LANDMARKS))
    roi_center, roi_rotation, vis_diff = [], [], []
    for kind, frames in sequences:
        a, b = PosePipeline(cpu), PosePipeline(npu)
        for i, frame in enumerate(frames):
            t0 = time.perf_counter()
            la = a.detect(frame, i * FRAME_MS + 1)
            t1 = time.perf_counter()
            lb = b.detect(frame, i * FRAME_MS + 1)
            t2 = time.perf_counter()
            timings["onnx"].append((t1 - t0) * 1000)
            timings["npu"].append((t2 - t1) * 1000)
            counts["frames"] += 1
            counts["agree"] += (la is None) == (lb is None)
            if not (la and lb):
                continue
            counts["pose_frames"] += 1
            counts["tracked_frames"] += not (a.detector_ran or b.detector_ran)
            xa = np.array([[p.x, p.y] for p in la])
            xb = np.array([[p.x, p.y] for p in lb])
            errors.extend(np.hypot(*(xa - xb).T).tolist())
            pa = np.array([p.visibility for p in la])
            pb = np.array([p.visibility for p in lb])
            same = (pa >= 0.5) == (pb >= 0.5)                  # FallDetector / face presence threshold
            counts["vis_agree"] += int(same.sum())
            counts["vis_total"] += same.size
            key_agree += same[list(KEY_LANDMARKS)]
            vis_diff.extend(np.abs(pa - pb).tolist())
            ra, rb = a.tracking_rect, b.tracking_rect          # next frame's ROI (from 33/34)
            roi_center.append(math.hypot(ra.x_center - rb.x_center, ra.y_center - rb.y_center))
            turn = abs(ra.rotation - rb.rotation) % (2 * math.pi)
            roi_rotation.append(math.degrees(min(turn, 2 * math.pi - turn)))
    stat = lambda values, q: round(float(np.percentile(values, q)), 5) if len(values) else None   # None fails
    return {"frames": counts["frames"], "pose_frames": counts["pose_frames"],
            "tracked_frames": counts["tracked_frames"], "landmarks_compared": len(errors),
            "detection_agreement": round(counts["agree"] / max(counts["frames"], 1), 4),
            "landmark_error_median": stat(errors, 50), "landmark_error_p95": stat(errors, 95),
            "landmark_error_p99": stat(errors, 99), "landmark_error_max": stat(errors, 100),
            "visibility_agreement": round(counts["vis_agree"] / counts["vis_total"], 4) if counts["vis_total"] else None,
            "visibility_agreement_key": ({str(i): round(float(n) / counts["pose_frames"], 4)
                                          for i, n in zip(KEY_LANDMARKS, key_agree)} if counts["pose_frames"] else None),
            "visibility_diff_p99": stat(vis_diff, 99), "visibility_diff_max": stat(vis_diff, 100),
            "tracking_roi_center_p95": stat(roi_center, 95), "tracking_roi_center_max": stat(roi_center, 100),
            "tracking_roi_rotation_max_deg": stat(roi_rotation, 100),
            "ms_per_frame_onnx_cpu": round(float(np.median(timings["onnx"])), 2),
            "ms_per_frame_npu": round(float(np.median(timings["npu"])), 2)}


def check_limits():
    return {"raw_landmark_xy_max": CHECK_RAW_XY_MAX, "raw_visibility_max": CHECK_RAW_VISIBILITY_MAX,
            "pose_flag_max": CHECK_POSE_FLAG_MAX, "pipeline_p95": CHECK_PIPELINE_P95,
            "pipeline_p99": CHECK_PIPELINE_P99, "detection_agreement": CHECK_DETECTION_AGREEMENT,
            "visibility_agreement": CHECK_VISIBILITY_AGREEMENT, "min_crops": CHECK_MIN_CROPS,
            "min_pose_frames": CHECK_MIN_POSE_FRAMES, "min_tracked_frames": CHECK_MIN_TRACKED_FRAMES}


def check_failures(raw, pipeline):
    """The limits a check result misses ([] = passed)."""
    tests = [("raw landmark x/y (0-34)", raw["landmark_xy_max"], "<=", CHECK_RAW_XY_MAX),
             ("raw visibility (0-32)", raw["visibility_max"], "<=", CHECK_RAW_VISIBILITY_MAX),
             ("raw pose flag", raw["pose_flag_max"], "<=", CHECK_POSE_FLAG_MAX),
             ("crops compared", raw["crops"], ">=", CHECK_MIN_CROPS),
             ("detection agreement", pipeline["detection_agreement"], ">=", CHECK_DETECTION_AGREEMENT),
             ("landmark error p95", pipeline["landmark_error_p95"], "<=", CHECK_PIPELINE_P95),
             ("landmark error p99", pipeline["landmark_error_p99"], "<=", CHECK_PIPELINE_P99),
             ("visibility agreement at 0.5", pipeline["visibility_agreement"], ">=", CHECK_VISIBILITY_AGREEMENT),
             ("frames with a pose in both", pipeline["pose_frames"], ">=", CHECK_MIN_POSE_FRAMES),
             ("tracked frames with a pose in both", pipeline["tracked_frames"], ">=", CHECK_MIN_TRACKED_FRAMES)]
    return [f"{name} {value} (limit {op} {limit})" for name, value, op, limit in tests
            if value is None or not (value <= limit if op == "<=" else value >= limit)]


def check(args):
    import traceback
    import numpy as np
    from features.pose.engines import OnnxEngine, RknnEngine
    directory = models.model_dir()
    manifest = models.load_manifest(directory)
    onnx_paths = models.verified_files(directory, manifest, "onnx")
    rknn_paths = models.verified_files(directory, manifest, "rknn")
    manifest.pop("check", None)
    models.write_manifest(directory, manifest)        # a crash below leaves no passed check behind
    sequences = check_sequences([sample_image(directory)] + [Path(p) for p in args.image], args.video,
                                args.max_frames, args.sequence_frames)
    frames = {kind: sum(len(f) for k, f in sequences if k == kind) for kind in ("still", "tracked", "video")}
    log(f"Checking on {sum(frames.values())} frames {frames} (NPU core {args.core}).")
    result = {"ran": _now(), "core": args.core, "rknn_toolkit_lite2": version_of("rknn-toolkit-lite2"),
              "kernel": platform.release(), "frames": frames, "limits": check_limits()}
    cpu = OnnxEngine(onnx_paths["pose_detector"], onnx_paths["pose_landmarks_detector"])
    npu = None
    try:
        npu = RknnEngine(rknn_paths["pose_detector"], rknn_paths["pose_landmarks_detector"], args.core)
        result["sdk"] = getattr(npu, "sdk_version", lambda: None)()
        result["raw"] = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in
                         compare_raw(cpu, npu, [f for _, fs in sequences for f in fs], np.random.default_rng(0)).items()}
        result["pipeline"] = compare_pipelines(cpu, npu, sequences)
        result["failures"] = check_failures(result["raw"], result["pipeline"])
    except Exception as error:                      # e.g. the NPU returned NaN: a failed check, recorded
        traceback.print_exc()
        result["failures"] = [f"error during the check: {type(error).__name__}: {error}"]
    finally:
        if npu is not None:
            npu.close()
        cpu.close()
    result["passed"] = not result["failures"]
    manifest["check"] = result
    models.write_manifest(directory, manifest)
    log(json.dumps(result, indent=2))
    log("Accuracy check PASSED: the runtime will use the NPU (BAYMAX_POSE_BACKEND=auto)." if result["passed"] else
        "Accuracy check FAILED (" + "; ".join(result["failures"]) +
        "): the runtime keeps MediaPipe on the CPU. See manifest.json.")
    return 0 if result["passed"] else 1


# ── all ──────────────────────────────────────────────────────────────────────
def run_all(args):
    directory = models.model_dir()
    venvs = build_dir(directory)
    onnx_python = venvs / "venv-onnx" / "bin" / "python"
    rknn_python = venvs / "venv-rknn" / "bin" / "python"
    missing = [str(p) for p in (onnx_python, rknn_python) if not p.exists()]
    if missing:
        log(f"Missing converter environments: {', '.join(missing)}. Run features/pose/setup_converter.sh first.")
        return 2
    steps = [(onnx_python, ["export-onnx"] + (["--task", args.task] if args.task else [])),
             (rknn_python, ["build-rknn", "--target", args.target]),
             (rknn_python, ["check", "--core", args.core] + sum((["--image", p] for p in args.image), [])
              + (["--video", args.video] if args.video else []) + ["--max-frames", str(args.max_frames),
                                                                       "--sequence-frames", str(args.sequence_frames)])]
    for python, step in steps:
        log(f"== {step[0]} ({python})")
        result = subprocess.run([str(python), str(Path(__file__).resolve()), *step])
        if result.returncode != 0:
            log(f"{step[0]} failed (exit {result.returncode}).")
            return result.returncode
    return 0


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


STEPS = ("all", "export-onnx", "build-rknn", "check")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="step")
    for step in STEPS:
        p = sub.add_parser(step)
        if step in ("all", "export-onnx"):
            p.add_argument("--task", help="pose_landmarker_full.task to convert (default: the runtime's copy or a download)")
        if step in ("all", "build-rknn"):
            p.add_argument("--target", default="rk3588")
            p.add_argument("--verbose", action="store_true")
        if step in ("all", "check"):
            p.add_argument("--core", default=os.environ.get("BAYMAX_POSE_NPU_CORE", "0"))
            p.add_argument("--image", action="append", default=[], help="extra still image(s) to check on")
            p.add_argument("--video", help="a local video (stays on the board) added to the gated check; its "
                                           "limits are set for the synthetic sequence, so real clips usually fail "
                                           "and keep Ember on the CPU: use validate.py --engine npu instead")
            p.add_argument("--max-frames", type=int, default=300, help="most --video frames to use")
            p.add_argument("--sequence-frames", type=int, default=SEQUENCE_FRAMES,
                           help="length of the synthetic tracked sequence (0: none)")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in STEPS + ("-h", "--help"):
        argv = ["all"] + argv                      # no step given: run them all
    args = parser.parse_args(argv)
    return {"all": run_all, "export-onnx": export_onnx, "build-rknn": build_rknn, "check": check}[args.step](args) or 0


if __name__ == "__main__":
    sys.exit(main())
