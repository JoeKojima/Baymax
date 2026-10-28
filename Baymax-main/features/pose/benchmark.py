"""Per-frame pose time on this machine: MediaPipe on the CPU vs the port on the NPU.

    .venv/bin/python features/pose/benchmark.py [--frames 300] [--video clip.mp4] [--core 0]
                                                [--fps 15] [--empty]

Runs MediaPipe exactly as the camera thread does (same options, BGR->RGB, VIDEO mode)
and the NPU backend (features/pose pipeline + RKNN) on the same frames, after the same
fast-core pinning as the runtime, and prints milliseconds per frame (median/p90/p99),
the process CPU time per frame (all threads), the NPU time split into detector /
landmark inference and CPU pre/post-processing, and how far the NPU landmarks are from
MediaPipe's. Frames come from --video (a local file; nothing leaves the machine), the
converter's sample image shifted a little each frame (tracking behaves as with a person
in view) or, with --empty, an empty noisy room (nobody in view: the detector runs on
every frame). Never opens a camera. ONNX Runtime is added when installed. The NPU part
needs the converted models (convert_rknn.py).

By default frames run back to back, which keeps the CPU and NPU clocked up: that is
throughput. --fps 15 paces them like the camera thread (it sleeps to each frame's
slot), so the clocks drop between frames as in Ember and each frame takes longer; this
is what the Monitor's inference time shows, and the CPU share is per core at that rate.
"""
import argparse
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Timed:
    """Wraps an engine and records how long each network takes."""

    def __init__(self, engine):
        self.engine = engine
        self.ms = {"detector": [], "landmarks": []}

    def detector(self, crop):
        t0 = time.perf_counter()
        out = self.engine.detector(crop)
        self.ms["detector"].append((time.perf_counter() - t0) * 1000)
        return out

    def landmarks(self, crop):
        t0 = time.perf_counter()
        out = self.engine.landmarks(crop)
        self.ms["landmarks"].append((time.perf_counter() - t0) * 1000)
        return out

    def close(self):
        self.engine.close()


def frames_from(args, directory):
    import cv2
    import numpy as np
    frames = []
    if args.empty:
        rng = np.random.default_rng(0)
        room = np.full((480, 640, 3), 90, np.int16)
        frames = [np.clip(room + rng.integers(-3, 4, room.shape), 0, 255).astype(np.uint8)
                  for _ in range(args.frames)]
    elif args.video:
        capture = cv2.VideoCapture(args.video)
        while len(frames) < args.frames:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(cv2.resize(frame, (640, 480)))
        capture.release()
    else:
        image_path = directory / "build" / "pose.jpg"
        if not image_path.is_file():
            from features.pose.convert_rknn import sample_image
            image_path = sample_image(directory)
        image = cv2.imread(str(image_path))
        h, w = image.shape[:2]
        scale = 440 / h
        image = cv2.resize(image, (int(w * scale), 440))
        for i in range(args.frames):
            canvas = np.full((480, 640, 3), 40, np.uint8)
            dx = int(20 * math.sin(i / 15))             # sway left and right
            x0 = max(0, (640 - image.shape[1]) // 2 + dx)
            part = image[:, : 640 - x0]
            canvas[20:20 + part.shape[0], x0:x0 + part.shape[1]] = part
            frames.append(canvas)
    if not frames:
        raise SystemExit("No frames to benchmark.")
    return frames


def run(backend, frames, warmup, fps=0, sleep=time.sleep, clock=time.monotonic):
    """Landmarks, wall ms and process CPU ms (all threads) per frame after the warm-up.
    fps > 0 starts each frame in its own 1/fps slot, like the camera thread."""
    import numpy as np
    out, ms, cpu = [], [], []
    interval = 1.0 / fps if fps and fps > 0 else 0.0
    next_slot = clock()
    for i, frame in enumerate(frames):
        if interval:
            wait = next_slot - clock()
            if wait > 0:
                sleep(wait)
            next_slot = max(next_slot + interval, clock())
        t0, c0 = time.perf_counter(), time.process_time()
        landmarks = backend.detect(frame, 1 + i * 66)
        elapsed, used = (time.perf_counter() - t0) * 1000, (time.process_time() - c0) * 1000
        if i >= warmup:
            ms.append(elapsed)
            cpu.append(used)
            out.append(None if landmarks is None else np.array([[p.x, p.y, p.visibility] for p in landmarks]))
    return out, ms, cpu


def summary(name, ms, found, cpu=(), fps=0):
    import numpy as np
    p = lambda q: float(np.percentile(ms, q)) if ms else float("nan")
    cpu_ms = float(np.mean(cpu)) if len(cpu) else float("nan")
    share = (f"= {cpu_ms * fps / 10:4.1f}% of a core at {fps:g} fps" if fps and fps > 0 else
             "(back to back)")
    print(f"{name:<28} median {p(50):6.1f} ms  p90 {p(90):6.1f}  p99 {p(99):6.1f}  "
          f"({1000 / p(50) if ms else 0:5.1f} fps at most)  process CPU {cpu_ms:5.1f} ms/frame {share}  "
          f"pose in {found}/{len(ms)} frames", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--video")
    parser.add_argument("--empty", action="store_true", help="nobody in view: the detector runs every frame")
    parser.add_argument("--fps", type=float, default=0,
                        help="pace frames like the camera thread (15 in Ember); default 0 = back to back")
    parser.add_argument("--core", default=None, help="NPU core: 0, 1, 2, 0_1, 0_1_2 or auto (default BAYMAX_POSE_NPU_CORE or 0)")
    parser.add_argument("--task", default=str(ROOT / "pose_landmarker_full.task"))
    parser.add_argument("--no-pin", action="store_true", help="don't pin to the fast cores like the runtime")
    args = parser.parse_args(argv)

    import os
    import platform
    import numpy as np
    import pc_hardware
    from features.pose import models
    from features.pose.backend import MediaPipeCpuBackend, PipelineBackend, default_mediapipe_options
    if not args.no_pin:
        pc_hardware.use_performance_cpus()
    directory = models.model_dir()
    frames = frames_from(args, directory)
    print(f"{len(frames)} frames ({args.warmup} warm-up) on {platform.machine()} {platform.release()}, "
          f"{'RK3588' if pc_hardware.is_rk3588() else 'not an RK3588'}, "
          f"{f'paced at {args.fps:g} fps' if args.fps > 0 else 'back to back'}", flush=True)

    results = {}
    if Path(args.task).is_file():
        cpu = MediaPipeCpuBackend(default_mediapipe_options(args.task))
        results["CPU (MediaPipe)"] = run(cpu, frames, args.warmup, args.fps)
        cpu.close()
    else:
        print(f"MediaPipe skipped: {args.task} not found (start Ember once to download it).")

    manifest = None
    try:
        manifest = models.load_manifest(directory)
    except models.ModelError as error:
        print(f"Port skipped: {error}")
    timed = {}
    if manifest is not None:
        try:
            from features.pose.engines import RknnEngine
            paths = models.verified_files(directory, manifest, "rknn")
            core = args.core or os.environ.get("BAYMAX_POSE_NPU_CORE", "0")
            timed["npu"] = Timed(RknnEngine(paths["pose_detector"], paths["pose_landmarks_detector"], core))
            results[f"NPU (RK3588, core {core})"] = run(PipelineBackend(timed["npu"], "npu", "NPU"), frames,
                                                        args.warmup, args.fps)
            timed["npu"].close()
        except Exception as error:
            print(f"NPU skipped: {type(error).__name__}: {error}")
        try:
            from features.pose.engines import OnnxEngine
            paths = models.verified_files(directory, manifest, "onnx")
            timed["onnx"] = Timed(OnnxEngine(paths["pose_detector"], paths["pose_landmarks_detector"]))
            results["CPU (ONNX Runtime port)"] = run(PipelineBackend(timed["onnx"], "onnx", "CPU"), frames,
                                                     args.warmup, args.fps)
            timed["onnx"].close()
        except ImportError:
            pass
        except Exception as error:
            print(f"ONNX Runtime skipped: {type(error).__name__}: {error}")

    print()
    for name, (landmarks, ms, cpu) in results.items():
        summary(name, ms, sum(x is not None for x in landmarks), cpu, args.fps)
    for key, label in (("npu", "NPU"), ("onnx", "ONNX Runtime")):
        if key in timed:
            t = timed[key].ms
            med = lambda values: float(np.median(values)) if values else float("nan")
            print(f"  {label}: detector {med(t['detector']):.1f} ms ({len(t['detector'])} runs), "
                  f"landmarks {med(t['landmarks']):.1f} ms ({len(t['landmarks'])} runs); the rest of each frame "
                  f"is CPU pre/post-processing")
    reference = results.get("CPU (MediaPipe)")
    if reference:
        for name, (landmarks, _, _) in results.items():
            if name == "CPU (MediaPipe)":
                continue
            errors = [np.hypot(*(a[:, :2] - b[:, :2]).T) for a, b in zip(reference[0], landmarks)
                      if a is not None and b is not None]
            agree = sum((a is None) == (b is None) for a, b in zip(reference[0], landmarks))
            if errors:
                errors = np.concatenate(errors)
                print(f"{name} vs MediaPipe: pose found agreement {agree}/{len(landmarks)}, landmark error "
                      f"median {np.median(errors):.4f} p95 {np.percentile(errors, 95):.4f} (normalised)")


if __name__ == "__main__":
    main()
