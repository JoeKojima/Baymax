"""Compare the pose port with MediaPipe on the same frames (development tool).

    python -m features.pose.validate --task pose_landmarker_full.task --onnx-dir .models/pose_npu \
        clip1.webm clip2.webm ... [--fps 15] [--seconds 60] [--falls clip.webm ...] [--json out.json]

Each clip is centre-cropped to 4:3, resized to 640x480 (the robot camera's format) and
sampled at --fps with the clip's own timestamps. Three runs see the frames:

    mediapipe   MediaPipe PoseLandmarker, VIDEO mode, the runtime's options (the reference)
    port        features.pose.pipeline on ONNX Runtime (the NPU backend's code path), or
                with --engine npu on the Orange Pi's NPU (the .rknn files in --onnx-dir)
    baseline    MediaPipe again on the same frames plus +-1 grey level of noise

The baseline shows how much MediaPipe itself changes for an invisible input change; the
port is judged against that. Reported for port and baseline vs the reference: detection
agreement, per-landmark error (Euclidean, normalised x/y) for 0, 2, 5, 11, 12, 23, 24,
visibility agreement at 0.5, face presence (0, 2, 5 >= 0.5) agreement, FallDetector
decisions with the runtime's settings on a simulated clock, and CPU time per frame.
--falls clips also get synthetic falls: the frames are rotated about the feet over time
(fast falls, a slow lie-down, a fall and get-up). Needs mediapipe, onnxruntime and
opencv; writes nothing but --json.
"""
import argparse
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KEY_LANDMARKS = (0, 2, 5, 11, 12, 23, 24)
FALL_SETTINGS = dict(angle_threshold=60.0, ang_vel_threshold=25.0, hip_vel_threshold=0.12, history_window=8,
                     confirmation_frames=2, cooldown_seconds=3.0, stay_down_s=2.0)   # realtime_gemini_10.py
RUNS = ("mediapipe", "port", "baseline")


def to_camera(frame, size=(640, 480)):
    h, w = frame.shape[:2]
    target = size[0] / size[1]
    if w / h > target:
        crop = int(round(h * target))
        x0 = (w - crop) // 2
        frame = frame[:, x0:x0 + crop]
    elif w / h < target:
        crop = int(round(w / target))
        y0 = (h - crop) // 2
        frame = frame[y0:y0 + crop]
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


def read_clip(path, fps, seconds, start=0.0):
    capture = cv2.VideoCapture(str(path))
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(source_fps / fps)))
    frames, index = [], 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        t = index / source_fps
        if t >= start and (index % step == 0):
            frames.append((int(round((t - start) * 1000)) + 1, to_camera(frame)))
            if seconds and t - start >= seconds:
                break
        index += 1
    capture.release()
    return frames


def with_noise(frames, seed=0):
    rng = np.random.default_rng(seed)
    return [(t, np.clip(f.astype(np.int16) + rng.integers(-1, 2, f.shape), 0, 255).astype(np.uint8))
            for t, f in frames]


class SimClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def fall_detector(clock):
    from fall_detection import detector as module
    module.time = SimpleNamespace(monotonic=clock.monotonic)   # the detector reads time.monotonic()
    return module.FallDetector(**FALL_SETTINGS)


def run_backend(factory, frames):
    backend = factory()
    clock = SimClock()
    detector = fall_detector(clock)
    landmarks, ms, falls = [], [], []
    for timestamp, frame in frames:
        t0 = time.perf_counter()
        result = backend.detect(frame, timestamp)
        ms.append((time.perf_counter() - t0) * 1000)
        clock.now = timestamp / 1000.0
        fall = detector.update(result)
        landmarks.append(None if result is None else
                         np.array([[p.x, p.y, p.z, p.visibility, p.presence] for p in result], np.float64))
        falls.append(fall)
    backend.close()
    return {"landmarks": landmarks, "ms": ms, "falls": falls}


def run_all(frames, factories, seed=0):
    noisy = with_noise(frames, seed)
    return {"mediapipe": run_backend(factories["mediapipe"], frames),
            "port": run_backend(factories["port"], frames),
            "baseline": run_backend(factories["mediapipe"], noisy)}


def compare(a, b):
    """b against the reference a."""
    n = len(a["landmarks"])
    both = [i for i in range(n) if a["landmarks"][i] is not None and b["landmarks"][i] is not None]
    stats = {"frames": n, "ref_found": sum(x is not None for x in a["landmarks"]),
             "found": sum(x is not None for x in b["landmarks"]),
             "detection_agree": sum((a["landmarks"][i] is None) == (b["landmarks"][i] is None) for i in range(n)),
             "both": len(both), "errors": {}, "visibility_agree": {}}
    for j in KEY_LANDMARKS:
        stats["errors"][j] = [math.hypot(*(a["landmarks"][i][j, :2] - b["landmarks"][i][j, :2])) for i in both]
        stats["visibility_agree"][j] = sum((a["landmarks"][i][j, 3] >= 0.5) == (b["landmarks"][i][j, 3] >= 0.5)
                                           for i in both)
    face = lambda lm: lm is not None and all(lm[j, 3] >= 0.5 for j in (0, 2, 5))
    stats["face_agree"] = sum(face(a["landmarks"][i]) == face(b["landmarks"][i]) for i in range(n))
    stats["face_ref"] = sum(face(x) for x in a["landmarks"])
    stats["face"] = sum(face(x) for x in b["landmarks"])
    stats["pending_agree"] = sum(bool(x["fall_pending"]) == bool(y["fall_pending"]) for x, y in zip(a["falls"], b["falls"]))
    stats["angle_diffs"] = [abs(x["trunk_angle"] - y["trunk_angle"]) for x, y in zip(a["falls"], b["falls"])
                            if x["trunk_angle"] is not None and y["trunk_angle"] is not None]
    stats["falls_ref"] = [i for i, f in enumerate(a["falls"]) if f["fall_detected"]]
    stats["falls"] = [i for i, f in enumerate(b["falls"]) if f["fall_detected"]]
    stats["ms_ref"], stats["ms"] = a["ms"], b["ms"]
    return stats


def rotate_about(frame, angle_deg, pivot):
    matrix = cv2.getRotationMatrix2D(pivot, angle_deg, 1.0)
    return cv2.warpAffine(frame, matrix, (frame.shape[1], frame.shape[0]), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT)


def synthetic_falls(frames, pivot, fps):
    """(name, frames, expect_fall): upright 1.5 s, then rotate by 90 degrees, then hold or get up."""
    def build(name, fall_s, hold_s, recover_s, direction, expect):
        upright, sequence = int(1.5 * fps), []
        total = upright + int((fall_s + hold_s + recover_s) * fps)
        for k in range(min(total, len(frames))):
            timestamp, frame = frames[k]
            t = (k - upright) / fps
            if t <= 0:
                angle = 0.0
            elif t < fall_s:
                angle = 90.0 * t / fall_s
            elif t < fall_s + hold_s:
                angle = 90.0
            else:
                angle = 90.0 * max(0.0, 1 - (t - fall_s - hold_s) / max(recover_s, 1e-6))
            sequence.append((timestamp, rotate_about(frame, direction * angle, pivot)))
        return name, sequence, expect
    return [build("fast fall left (0.4 s)", 0.4, 4.0, 0, 1, True),
            build("fast fall right (0.6 s)", 0.6, 4.0, 0, -1, True),
            build("slow lie-down (4 s)", 4.0, 3.0, 0, 1, False),
            build("fall and get up within 1 s", 0.4, 0.8, 0.6, -1, False)]


def pct(values, q):
    return float(np.percentile(values, q)) if len(values) else float("nan")


def summarize(group):
    keys = ("frames", "ref_found", "found", "detection_agree", "both", "face_agree", "face_ref", "face",
            "pending_agree")
    total = {k: sum(s[k] for s in group) for k in keys}
    total["errors"] = {j: sum((s["errors"][j] for s in group), []) for j in KEY_LANDMARKS}
    total["visibility_agree"] = {j: sum(s["visibility_agree"][j] for s in group) for j in KEY_LANDMARKS}
    total["angle_diffs"] = sum((s["angle_diffs"] for s in group), [])
    total["fall_events_ref"] = sum(len(s["falls_ref"]) for s in group)
    total["fall_events"] = sum(len(s["falls"]) for s in group)
    total["ms_ref"] = sum((s["ms_ref"] for s in group), [])
    total["ms"] = sum((s["ms"] for s in group), [])
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("clips", nargs="*")
    parser.add_argument("--task", required=True)
    parser.add_argument("--onnx-dir", required=True)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--falls", nargs="*", default=[], help="clips with an upright person for synthetic falls")
    parser.add_argument("--fall-starts", default="0,25", help="seconds into each --falls clip to start from")
    parser.add_argument("--json")
    parser.add_argument("--engine", choices=("onnx", "npu"), default="onnx",
                        help="run the port on ONNX Runtime (CPU) or, on the Orange Pi, on the NPU "
                             "(the .rknn files in --onnx-dir)")
    parser.add_argument("--core", default="0", help="NPU core for --engine npu")
    args = parser.parse_args(argv)

    from features.pose.backend import MediaPipeCpuBackend, PipelineBackend, default_mediapipe_options
    from features.pose.engines import OnnxEngine, RknnEngine
    onnx_dir = Path(args.onnx_dir)
    if args.engine == "npu":
        def port():
            return PipelineBackend(RknnEngine(onnx_dir / "pose_detector.rknn",
                                              onnx_dir / "pose_landmarks_detector.rknn", core=args.core), "port", "NPU")
    else:
        def port():
            return PipelineBackend(OnnxEngine(onnx_dir / "pose_detector.onnx",
                                              onnx_dir / "pose_landmarks_detector.onnx"), "port", "CPU")
    factories = {
        "mediapipe": lambda: MediaPipeCpuBackend(default_mediapipe_options(args.task)),
        "port": port,
    }
    report = {"clips": {}, "falls": {}}
    groups = {"clips": {"port": [], "baseline": []}, "falls": {"port": [], "baseline": []}}
    for clip in args.clips:
        runs = run_all(read_clip(clip, args.fps, args.seconds), factories)
        entry = {kind: compare(runs["mediapipe"], runs[kind]) for kind in ("port", "baseline")}
        report["clips"][Path(clip).name] = entry
        for kind in entry:
            groups["clips"][kind].append(entry[kind])
        _print_clip(Path(clip).name, entry)
    starts = [float(x) for x in args.fall_starts.split(",") if x.strip()]
    for clip, start in [(clip, start) for clip in args.falls for start in starts]:
        frames = read_clip(clip, args.fps, 12.0, start)
        if len(frames) < 12 * args.fps:
            continue
        first = run_backend(factories["mediapipe"], frames[:1])["landmarks"][0]
        pivot = (320.0, 470.0)
        if first is not None:
            ankles = first[[27, 28], :2].mean(0) * (640, 480)
            pivot = (float(np.clip(ankles[0], 0, 639)), float(np.clip(ankles[1], 0, 479)))
        for name, sequence, expect in synthetic_falls(frames, pivot, args.fps):
            runs = run_all(sequence, factories)
            entry = {kind: compare(runs["mediapipe"], runs[kind]) for kind in ("port", "baseline")}
            entry["expect_fall"] = expect
            key = f"{Path(clip).name}@{start:g}s: {name}"
            report["falls"][key] = entry
            for kind in ("port", "baseline"):
                groups["falls"][kind].append(entry[kind])
            print(f"{key}: expect {'fall' if expect else 'no fall'} | falls at frame: MediaPipe "
                  f"{entry['port']['falls_ref']} port {entry['port']['falls']} baseline {entry['baseline']['falls']} "
                  f"| pending agree port {entry['port']['pending_agree']}/{entry['port']['frames']} "
                  f"baseline {entry['baseline']['pending_agree']}", flush=True)
    for label, key in (("ALL CLIPS", "clips"), ("SYNTHETIC FALLS", "falls")):
        if groups[key]["port"]:
            _print_summary(label, summarize(groups[key]["port"]), summarize(groups[key]["baseline"]))
    if args.json:
        Path(args.json).write_text(json.dumps(report, default=_jsonable), encoding="utf-8")


def _jsonable(value):
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    raise TypeError(type(value))


def _print_clip(name, entry):
    for kind in ("port", "baseline"):
        s = entry[kind]
        errs = " ".join(f"{j}:{pct(s['errors'][j], 50):.4f}/{pct(s['errors'][j], 95):.4f}" for j in KEY_LANDMARKS)
        print(f"{name} [{kind}]: frames {s['frames']} | found ref {s['ref_found']} this {s['found']} agree "
              f"{s['detection_agree']} | err med/p95 {errs} | face agree {s['face_agree']} | falls ref "
              f"{len(s['falls_ref'])} this {len(s['falls'])} | ms ref {pct(s['ms_ref'], 50):.1f} this "
              f"{pct(s['ms'], 50):.1f}", flush=True)


def _print_summary(label, port, base):
    print(f"\n== {label}: {port['frames']} frames")
    print("| | port vs MediaPipe | MediaPipe+noise vs MediaPipe |")
    print("|---|---|---|")
    for title, key in (("pose found / reference found", None),):
        print(f"| {title} | {port['found']} / {port['ref_found']} | {base['found']} / {base['ref_found']} |")
    print(f"| detection agreement | {_rate(port['detection_agree'], port['frames'])} | "
          f"{_rate(base['detection_agree'], base['frames'])} |")
    for j in KEY_LANDMARKS:
        print(f"| landmark {j} error median / p95 | {pct(port['errors'][j], 50):.4f} / {pct(port['errors'][j], 95):.4f} | "
              f"{pct(base['errors'][j], 50):.4f} / {pct(base['errors'][j], 95):.4f} |")
    for j in KEY_LANDMARKS:
        print(f"| landmark {j} visibility agreement @0.5 | {_rate(port['visibility_agree'][j], port['both'])} | "
              f"{_rate(base['visibility_agree'][j], base['both'])} |")
    print(f"| face presence agreement | {_rate(port['face_agree'], port['frames'])} | "
          f"{_rate(base['face_agree'], base['frames'])} |")
    print(f"| trunk angle abs diff median / p95 (deg) | {pct(port['angle_diffs'], 50):.2f} / "
          f"{pct(port['angle_diffs'], 95):.2f} | {pct(base['angle_diffs'], 50):.2f} / {pct(base['angle_diffs'], 95):.2f} |")
    print(f"| fall events (reference: {port['fall_events_ref']}) | {port['fall_events']} | {base['fall_events']} |")
    print(f"| fall-pending frame agreement | {_rate(port['pending_agree'], port['frames'])} | "
          f"{_rate(base['pending_agree'], base['frames'])} |")
    print(f"CPU ms/frame median (p95): MediaPipe {pct(port['ms_ref'], 50):.1f} ({pct(port['ms_ref'], 95):.1f}), "
          f"port on ONNX Runtime {pct(port['ms'], 50):.1f} ({pct(port['ms'], 95):.1f})")


def _rate(count, total):
    return f"{count}/{total} ({100 * count / max(total, 1):.2f}%)"


if __name__ == "__main__":
    main()
