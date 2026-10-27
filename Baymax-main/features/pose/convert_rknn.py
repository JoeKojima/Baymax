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
    inputs: raw outputs, then the whole pose pipeline on sample frames. The runtime
    uses the NPU automatically only after this check has passed.

Everything is written to the model directory (default Baymax-main/.models/pose_npu,
BAYMAX_POSE_MODEL_DIR overrides). Nothing is uploaded; downloads are the .task and a
MediaPipe sample image, both checked against SHA-256s recorded below.
"""
import argparse
import datetime
import json
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
# NPU vs ONNX acceptance (FP16 on the NPU): raw landmark x/y in 256-px crop units, and the
# whole pipeline in normalised image coordinates. On the validation clips MediaPipe itself
# moves by p95 0.005-0.01 when +-1 grey level of noise is added (VALIDATION.md).
CHECK_RAW_XY_MAX = 3.0
CHECK_PIPELINE_P95 = 0.01
CHECK_PIPELINE_P99 = 0.03
CHECK_DETECTION_AGREEMENT = 0.98


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
            except RuntimeError as error:
                if explicit:
                    raise
                log(f"Ignoring {candidate}: {error}")
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


# ── check ────────────────────────────────────────────────────────────────────
def _frames(image_paths, video, limit):
    import cv2
    frames = []
    for path in image_paths:
        image = cv2.imread(str(path))
        if image is None:
            raise RuntimeError(f"cannot read {path}")
        frames += [image, cv2.flip(image, 1), cv2.convertScaleAbs(image, alpha=0.6, beta=0)]
    if video:
        capture = cv2.VideoCapture(str(video))
        while len(frames) < limit:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
        capture.release()
    return frames


def check(args):
    import time
    import numpy as np
    from features.pose.engines import OnnxEngine, RknnEngine
    from features.pose.pipeline import PosePipeline, warp, LANDMARK_SIZE
    import cv2
    directory = models.model_dir()
    manifest = models.load_manifest(directory)
    onnx_paths = models.verified_files(directory, manifest, "onnx")
    rknn_paths = models.verified_files(directory, manifest, "rknn")
    cpu = OnnxEngine(onnx_paths["pose_detector"], onnx_paths["pose_landmarks_detector"])
    npu = RknnEngine(rknn_paths["pose_detector"], rknn_paths["pose_landmarks_detector"], args.core)
    images = [sample_image(directory)] + [Path(p) for p in args.image]
    frames = _frames(images, args.video, args.max_frames)
    log(f"Checking on {len(frames)} frames (NPU core {args.core}).")

    # 1. Raw outputs on identical crops.
    raw = {"detector_box_max": 0.0, "detector_score_max": 0.0, "landmark_xy_max": 0.0,
           "pose_flag_max": 0.0, "heatmap_max": 0.0}
    rng = np.random.default_rng(0)
    finder = PosePipeline(cpu)
    for frame in frames + [rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)]:
        crop = cv2.resize(frame, (224, 224))[:, :, ::-1].copy()
        (b1, s1), (b2, s2) = cpu.detector(crop), npu.detector(crop)
        confident = s1[:, 0] > -2.0               # boxes near the detection threshold matter most
        if confident.any():
            raw["detector_box_max"] = max(raw["detector_box_max"], float(np.abs(b1 - b2)[confident].max()))
        raw["detector_score_max"] = max(raw["detector_score_max"], float(np.abs(s1 - s2)[confident].max())
                                        if confident.any() else 0.0)
        rect = finder.detect_rect(frame)
        if rect is None:
            continue
        h, w = frame.shape[:2]
        crop = warp(frame, (rect.x_center * w, rect.y_center * h, rect.width * w, rect.height * h, rect.rotation),
                    LANDMARK_SIZE, cv2.BORDER_REPLICATE)[:, :, ::-1].copy()
        (r1, f1, h1), (r2, f2, h2) = cpu.landmarks(crop), npu.landmarks(crop)
        xy1, xy2 = r1.reshape(39, 5)[:33, :2], r2.reshape(39, 5)[:33, :2]
        raw["landmark_xy_max"] = max(raw["landmark_xy_max"], float(np.abs(xy1 - xy2).max()))
        raw["pose_flag_max"] = max(raw["pose_flag_max"], float(abs(f1[0] - f2[0])))
        raw["heatmap_max"] = max(raw["heatmap_max"], float(np.abs(h1 - h2).max()))

    # 2. The whole pipeline, frame by frame (a fresh tracker per still image).
    errors, agree, total, vis_agree, vis_total = [], 0, 0, 0, 0
    timings = {"onnx": [], "npu": []}
    sequences = [[f] for f in frames[:3 * len(images)]] + ([frames[3 * len(images):]] if args.video else [])
    for sequence in sequences:
        a, b = PosePipeline(cpu), PosePipeline(npu)
        for i, frame in enumerate(sequence):
            t0 = time.perf_counter()
            la = a.detect(frame, i * 66 + 1)
            t1 = time.perf_counter()
            lb = b.detect(frame, i * 66 + 1)
            t2 = time.perf_counter()
            timings["onnx"].append((t1 - t0) * 1000)
            timings["npu"].append((t2 - t1) * 1000)
            total += 1
            agree += (la is None) == (lb is None)
            if la and lb:
                xa = np.array([[p.x, p.y] for p in la])
                xb = np.array([[p.x, p.y] for p in lb])
                errors.extend(np.hypot(*(xa - xb).T).tolist())
                va = np.array([p.visibility >= 0.5 for p in la])
                vb = np.array([p.visibility >= 0.5 for p in lb])
                vis_agree += int((va == vb).sum())
                vis_total += va.size
    errors = np.array(errors) if errors else np.zeros(1)
    pipeline = {"frames": total, "detection_agreement": round(agree / max(total, 1), 4),
                "landmark_error_median": round(float(np.median(errors)), 5),
                "landmark_error_p95": round(float(np.percentile(errors, 95)), 5),
                "landmark_error_p99": round(float(np.percentile(errors, 99)), 5),
                "landmark_error_max": round(float(errors.max()), 5),
                "visibility_agreement": round(vis_agree / max(vis_total, 1), 4),
                "ms_per_frame_onnx_cpu": round(float(np.median(timings["onnx"])), 2),
                "ms_per_frame_npu": round(float(np.median(timings["npu"])), 2)}
    passed = (raw["landmark_xy_max"] <= CHECK_RAW_XY_MAX
              and pipeline["detection_agreement"] >= CHECK_DETECTION_AGREEMENT
              and pipeline["landmark_error_p95"] <= CHECK_PIPELINE_P95
              and pipeline["landmark_error_p99"] <= CHECK_PIPELINE_P99)
    try:
        sdk = npu.runtimes["pose_detector"].get_sdk_version()
    except Exception as error:
        sdk = f"unavailable ({type(error).__name__})"
    npu.close()
    cpu.close()
    manifest["check"] = {"passed": passed, "ran": _now(), "core": args.core,
                         "rknn_toolkit_lite2": version_of("rknn-toolkit-lite2"), "sdk": str(sdk).strip(),
                         "kernel": platform.release(), "raw": {k: round(v, 5) for k, v in raw.items()},
                         "pipeline": pipeline,
                         "limits": {"raw_landmark_xy_max": CHECK_RAW_XY_MAX, "pipeline_p95": CHECK_PIPELINE_P95,
                                    "pipeline_p99": CHECK_PIPELINE_P99,
                                    "detection_agreement": CHECK_DETECTION_AGREEMENT}}
    models.write_manifest(directory, manifest)
    log(json.dumps(manifest["check"], indent=2))
    log("Accuracy check PASSED: the runtime will use the NPU (BAYMAX_POSE_BACKEND=auto)." if passed else
        "Accuracy check FAILED: the runtime keeps MediaPipe on the CPU. See manifest.json.")
    return 0 if passed else 1


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
              + (["--video", args.video] if args.video else []) + ["--max-frames", str(args.max_frames)])]
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
            p.add_argument("--video", help="a local video to check on (stays on the board)")
            p.add_argument("--max-frames", type=int, default=300)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in STEPS + ("-h", "--help"):
        argv = ["all"] + argv                      # no step given: run them all
    args = parser.parse_args(argv)
    return {"all": run_all, "export-onnx": export_onnx, "build-rknn": build_rknn, "check": check}[args.step](args) or 0


if __name__ == "__main__":
    sys.exit(main())
