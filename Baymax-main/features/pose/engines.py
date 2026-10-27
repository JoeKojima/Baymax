"""Engines that run the two pose networks for pipeline.PosePipeline.

Both take uint8 RGB crops (NHWC, 224x224 for the detector, 256x256 for the
landmark model) and return float32 arrays:

    detector(crop)  -> (boxes (2254, 12), scores (2254, 1))   raw, before sigmoid
    landmarks(crop) -> (raw (195,), pose_flag (1,), heatmap (64, 64, 39))

OnnxEngine normalises like MediaPipe's ImageToTensor (detector [-1, 1], landmarks
[0, 1]) and runs ONNX Runtime on the CPU. RknnEngine hands the uint8 crop to the
NPU, where the conversion baked the same mean/std in. `rknnlite` is imported only
inside RknnEngine, i.e. only when the NPU backend is chosen on Linux aarch64.
"""
import numpy as np

# Input value ranges of the two TFLite models (their metadata's mean/std):
# detector (x - 127.5) / 127.5, landmarks (x - 0) / 255.
NORMALIZATION = {
    "pose_detector": {"mean": 127.5, "std": 127.5, "size": 224},
    "pose_landmarks_detector": {"mean": 0.0, "std": 255.0, "size": 256},
}
DETECTOR_BOXES = 2254 * 12
DETECTOR_SCORES = 2254
LANDMARK_VALUES = 195
HEATMAP_VALUES = 64 * 64 * 39

# RKNNLite core_mask names (BAYMAX_POSE_NPU_CORE).
NPU_CORES = {"auto": "NPU_CORE_AUTO", "0": "NPU_CORE_0", "1": "NPU_CORE_1", "2": "NPU_CORE_2",
             "0_1": "NPU_CORE_0_1", "0_1_2": "NPU_CORE_0_1_2", "all": "NPU_CORE_0_1_2"}


def _by_size(outputs, *sizes):
    """Pick outputs by element count (engines may return them in any order)."""
    found = {}
    for output in outputs:
        array = np.asarray(output)
        if array.size in sizes and array.size not in found:
            found[array.size] = array
    missing = [size for size in sizes if size not in found]
    if missing:
        shapes = [np.asarray(o).shape for o in outputs]
        raise RuntimeError(f"Unexpected pose model outputs {shapes}; missing sizes {missing}")
    return [found[size] for size in sizes]


def heatmap_hwc(heatmap):
    """(64, 64, 39) whatever layout the engine returned (NHWC or NCHW)."""
    heatmap = np.asarray(heatmap, np.float32)
    if heatmap.ndim == 4 and heatmap.shape[1] == 39 and heatmap.shape[3] == 64:
        heatmap = heatmap.transpose(0, 2, 3, 1)
    return heatmap.reshape(64, 64, 39)


def rknnlite_class():
    """rknnlite.api.RKNNLite. Ember's only rknnlite import: called on Linux aarch64 only."""
    from rknnlite.api import RKNNLite
    return RKNNLite


def split_detector(outputs):
    boxes, scores = _by_size(outputs, DETECTOR_BOXES, DETECTOR_SCORES)
    return (np.asarray(boxes, np.float32).reshape(2254, 12),
            np.asarray(scores, np.float32).reshape(2254, 1))


def split_landmarks(outputs):
    raw, flag, heatmap = _by_size(outputs, LANDMARK_VALUES, 1, HEATMAP_VALUES)
    return (np.asarray(raw, np.float32).reshape(LANDMARK_VALUES),
            np.asarray(flag, np.float32).reshape(1), heatmap_hwc(heatmap))


class OnnxEngine:
    """ONNX Runtime on the CPU (validation, and BAYMAX_POSE_BACKEND=onnx)."""
    name = "ONNX Runtime (CPU)"

    def __init__(self, detector_path, landmarks_path, threads=None):
        import onnxruntime as ort
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = int(threads)
        self.sessions = {}
        for key, path in (("pose_detector", detector_path), ("pose_landmarks_detector", landmarks_path)):
            session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
            shape = session.get_inputs()[0].shape
            self.sessions[key] = (session, session.get_inputs()[0].name, shape[1] == 3)

    def _run(self, key, crop):
        session, input_name, nchw = self.sessions[key]
        norm = NORMALIZATION[key]
        # Same arithmetic as MediaPipe's convertTo(dst, CV_32F, scale, offset).
        scale = np.float32(1.0 / norm["std"])
        offset = np.float32(-norm["mean"] / norm["std"])
        tensor = crop.astype(np.float32)[None] * scale + offset
        if nchw:
            tensor = tensor.transpose(0, 3, 1, 2)
        return session.run(None, {input_name: np.ascontiguousarray(tensor)})

    def detector(self, crop):
        return split_detector(self._run("pose_detector", crop))

    def landmarks(self, crop):
        return split_landmarks(self._run("pose_landmarks_detector", crop))

    def close(self):
        self.sessions = {}


class RknnEngine:
    """The converted .rknn models on the RK3588 NPU through rknn-toolkit-lite2."""
    name = "RKNN (RK3588 NPU)"

    def __init__(self, detector_path, landmarks_path, core="0"):
        RKNNLite = rknnlite_class()
        core = str(core or "0").strip().lower()
        if core not in NPU_CORES:
            raise ValueError(f"Unknown NPU core {core!r}; use one of {', '.join(NPU_CORES)}")
        self.core_mask = getattr(RKNNLite, NPU_CORES[core])
        self.runtimes = {}
        try:
            for key, path in (("pose_detector", detector_path), ("pose_landmarks_detector", landmarks_path)):
                runtime = RKNNLite(verbose=False)
                self.runtimes[key] = runtime
                ret = runtime.load_rknn(str(path))
                if ret != 0:
                    raise RuntimeError(f"load_rknn({path}) returned {ret}")
                ret = runtime.init_runtime(core_mask=self.core_mask)
                if ret != 0:
                    raise RuntimeError(f"init_runtime({key}, core {core}) returned {ret}")
        except Exception:
            self.close()
            raise

    def _run(self, key, crop):
        outputs = self.runtimes[key].inference(inputs=[np.ascontiguousarray(crop)[None]], data_format="nhwc")
        if outputs is None:
            raise RuntimeError(f"RKNN inference returned nothing for {key}")
        return outputs

    def detector(self, crop):
        return split_detector(self._run("pose_detector", crop))

    def landmarks(self, crop):
        return split_landmarks(self._run("pose_landmarks_detector", crop))

    def close(self):
        for runtime in self.runtimes.values():
            try:
                runtime.release()
            except Exception:
                pass
        self.runtimes = {}
