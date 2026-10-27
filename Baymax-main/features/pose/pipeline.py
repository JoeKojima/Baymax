"""MediaPipe Pose Landmarker ("full", VIDEO mode, one pose) in numpy + OpenCV.

The two neural networks run on an engine (engines.py: ONNX Runtime on the CPU or
RKNN on the RK3588 NPU). Everything around them is ported from MediaPipe 0.10.35's
C++ graph so the landmarks behave like `PoseLandmarker.detect_for_video`:

- pose_detector_graph.cc: letterboxed 224x224 input (BORDER_ZERO), SSD anchors,
  sigmoid scores >= min_pose_detection_confidence, weighted NMS (IoU 0.5),
  letterbox removal, ROI from keypoints 0/1 (rotation, 2x distance), x1.25 square.
- pose_landmarker_graph.cc: the detector runs only while not tracking; the ROI
  for the next frame comes from the previous frame's auxiliary landmarks 33/34.
- pose_landmarks_detector_graph.cc: 256x256 warp (BORDER_REPLICATE), pose flag
  > min_pose_presence_confidence, sigmoid visibility/presence, heatmap
  refinement (kernel 7), projection back to the image, then (VIDEO mode)
  visibility low-pass (alpha 0.1) and a one-euro filter on x/y/z
  (min_cutoff 0.05, beta 80, derivate_cutoff 1, scaled by the ROI size).

The output is None or 33 `NormalizedLandmark`s with MediaPipe's conventions:
x/y normalised to the image (may fall outside 0..1), z on the x scale relative
to the hips, visibility/presence in 0..1. Numpy and OpenCV only.
"""
import math

import cv2
import numpy as np

DETECTOR_SIZE = 224
LANDMARK_SIZE = 256
NUM_LANDMARKS = 33
MODEL_LANDMARKS = 39                 # 33 pose + 2 auxiliary (ROI) + 4 unused
HEATMAP_KERNEL = 7
MIN_CONFIDENCE_TO_REFINE = 0.5
NMS_IOU = 0.5                         # PoseDetectorGraphOptions.min_suppression_threshold
ROI_SCALE = 1.25
SCORE_CLIP = 100.0
VISIBILITY_ALPHA = 0.1
ONE_EURO = dict(min_cutoff=0.05, beta=80.0, derivate_cutoff=1.0)

# SsdAnchorsCalculator options from pose_detector_graph.cc.
_ANCHOR_STRIDES = (8, 16, 32, 32, 32)
_ANCHOR_MIN_SCALE = 0.1484375
_ANCHOR_MAX_SCALE = 0.75


class NormalizedLandmark:
    """Same attributes as mediapipe.tasks...NormalizedLandmark."""
    __slots__ = ("x", "y", "z", "visibility", "presence", "name")

    def __init__(self, x, y, z, visibility=None, presence=None, name=None):
        self.x, self.y, self.z = x, y, z
        self.visibility, self.presence, self.name = visibility, presence, name

    def __repr__(self):
        return (f"NormalizedLandmark(x={self.x:.4f}, y={self.y:.4f}, z={self.z:.4f}, "
                f"visibility={self.visibility:.3f}, presence={self.presence:.3f})")


class Rect:
    """mediapipe NormalizedRect: centre and size normalised to the image, rotation in radians."""
    __slots__ = ("x_center", "y_center", "width", "height", "rotation")

    def __init__(self, x_center, y_center, width, height, rotation=0.0):
        self.x_center, self.y_center = float(x_center), float(y_center)
        self.width, self.height, self.rotation = float(width), float(height), float(rotation)

    def __repr__(self):
        return (f"Rect({self.x_center:.4f}, {self.y_center:.4f}, {self.width:.4f}, "
                f"{self.height:.4f}, rot={self.rotation:.4f})")


class Detection:
    __slots__ = ("score", "box", "keypoints")

    def __init__(self, score, box, keypoints):
        self.score = float(score)
        self.box = np.asarray(box, np.float64)              # xmin, ymin, xmax, ymax (relative)
        self.keypoints = np.asarray(keypoints, np.float64)  # (4, 2) relative x, y


# ── Detector ─────────────────────────────────────────────────────────────────
def ssd_anchors():
    """Anchor centres (2254, 2) for the 224x224 detector; fixed_anchor_size means w=h=1."""
    centres = []
    layer = 0
    while layer < len(_ANCHOR_STRIDES):
        per_cell = 0
        last = layer
        while last < len(_ANCHOR_STRIDES) and _ANCHOR_STRIDES[last] == _ANCHOR_STRIDES[layer]:
            per_cell += 2                    # aspect ratio 1.0 + the interpolated scale
            last += 1
        size = math.ceil(DETECTOR_SIZE / _ANCHOR_STRIDES[layer])
        for y in range(size):
            for x in range(size):
                centres.extend([((x + 0.5) / size, (y + 0.5) / size)] * per_cell)
        layer = last
    return np.asarray(centres, np.float32)


def normalize_radians(angle):
    return angle - 2 * math.pi * math.floor((angle + math.pi) / (2 * math.pi))


def detector_roi(width, height):
    """The full image letterboxed into the square detector input (ImageToTensor keep_aspect_ratio)."""
    roi_w, roi_h = float(width), float(height)
    tensor_aspect = 1.0                      # 224 / 224
    roi_aspect = roi_h / roi_w
    if tensor_aspect > roi_aspect:
        padding = (0.0, (1.0 - roi_aspect / tensor_aspect) / 2.0)
        roi_w, roi_h = roi_w, roi_w * tensor_aspect
    else:
        padding = ((1.0 - tensor_aspect / roi_aspect) / 2.0, 0.0)
        roi_w, roi_h = roi_h / tensor_aspect, roi_h
    roi = (width / 2.0, height / 2.0, roi_w, roi_h, 0.0)
    return roi, (padding[0], padding[1], padding[0], padding[1])   # left, top, right, bottom


def warp(image, roi, size, border):
    """cv::warpPerspective of a rotated ROI (pixels) to size x size, as MediaPipe's OpenCV converter."""
    cx, cy, w, h, rotation = roi
    points = cv2.boxPoints(((cx, cy), (w, h), rotation * 180.0 / math.pi))
    corners = np.array([[0, size], [0, 0], [size, 0], [size, size]], np.float32)
    matrix = cv2.getPerspectiveTransform(points.astype(np.float32), corners)
    return cv2.warpPerspective(image, matrix, (size, size), flags=cv2.INTER_LINEAR,
                               borderMode=border, borderValue=0)


def decode_detections(raw_boxes, raw_scores, anchors, min_score):
    """TensorsToDetectionsCalculator: XYWH boxes + 4 keypoints, scale 224, sigmoid scores."""
    raw = np.asarray(raw_boxes, np.float32).reshape(-1, 12)
    scores = np.clip(np.asarray(raw_scores, np.float32).reshape(-1), -SCORE_CLIP, SCORE_CLIP)
    with np.errstate(over="ignore"):                     # exp(100) -> inf -> score 0, as in C++
        scores = 1.0 / (1.0 + np.exp(-scores))
    keep = np.nonzero(scores >= min_score)[0]
    detections = []
    for i in keep:
        ax, ay = anchors[i]
        xc = raw[i, 0] / DETECTOR_SIZE + ax
        yc = raw[i, 1] / DETECTOR_SIZE + ay
        w = raw[i, 2] / DETECTOR_SIZE
        h = raw[i, 3] / DETECTOR_SIZE
        if not (w >= 0 and h >= 0):          # also drops NaN
            continue
        kps = raw[i, 4:12].reshape(4, 2) / DETECTOR_SIZE + (ax, ay)
        detections.append(Detection(scores[i], (xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2), kps))
    return detections


def _iou(box, boxes):
    """IoU of one box against many (xmin, ymin, xmax, ymax), 0 for disjoint boxes."""
    ix0 = np.maximum(box[0], boxes[:, 0])
    iy0 = np.maximum(box[1], boxes[:, 1])
    ix1 = np.minimum(box[2], boxes[:, 2])
    iy1 = np.minimum(box[3], boxes[:, 3])
    inter = np.clip(ix1 - ix0, 0, None) * np.clip(iy1 - iy0, 0, None)
    area = (box[2] - box[0]) * (box[3] - box[1])
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    union = area + areas - inter
    disjoint = (boxes[:, 2] < box[0]) | (box[2] < boxes[:, 0]) | (boxes[:, 3] < box[1]) | (box[3] < boxes[:, 1])
    with np.errstate(divide="ignore", invalid="ignore"):
        iou = np.where(union > 0, inter / union, 0.0)
    return np.where(disjoint, 0.0, iou)


def weighted_nms(detections, threshold=NMS_IOU):
    """NonMaxSuppressionCalculator WEIGHTED / INTERSECTION_OVER_UNION."""
    if not detections:
        return []
    order = sorted(range(len(detections)), key=lambda i: -detections[i].score)
    boxes = np.stack([d.box for d in detections])
    remaining = order
    output = []
    while remaining:
        top = detections[remaining[0]]
        similarity = _iou(top.box, boxes[remaining])
        candidates = [i for i, s in zip(remaining, similarity) if s > threshold]
        rest = [i for i, s in zip(remaining, similarity) if not s > threshold]
        merged = Detection(top.score, top.box, top.keypoints)
        if candidates:
            weights = np.array([detections[i].score for i in candidates])
            total = weights.sum()
            merged.box = (boxes[candidates] * weights[:, None]).sum(0) / total
            merged.keypoints = (np.stack([detections[i].keypoints for i in candidates])
                                * weights[:, None, None]).sum(0) / total
        output.append(merged)
        if len(rest) == len(remaining):
            break
        remaining = rest
    return output


def remove_letterbox(detection, padding):
    left, top, right, bottom = padding
    sx, sy = 1.0 - left - right, 1.0 - top - bottom
    box = detection.box
    xmin, ymin = (box[0] - left) / sx, (box[1] - top) / sy
    w, h = (box[2] - box[0]) / sx, (box[3] - box[1]) / sy
    kps = (detection.keypoints - (left, top)) / (sx, sy)
    return Detection(detection.score, (xmin, ymin, xmin + w, ymin + h), kps)


def alignment_rect(start, end, width, height):
    """AlignmentPointsRectsCalculator: centre at `start`, size 2x |end-start|, rotation to vertical."""
    x0, y0 = start[0] * width, start[1] * height
    x1, y1 = end[0] * width, end[1] * height
    box = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2) * 2.0
    rotation = normalize_radians(math.pi / 2 - math.atan2(-(y1 - y0), x1 - x0))
    return Rect(x0 / width, y0 / height, box / width, box / height, rotation)


def transform_rect(rect, width, height, scale=ROI_SCALE):
    """RectTransformationCalculator with scale 1.25 and square_long (no shift)."""
    long_side = max(rect.width * width, rect.height * height)
    return Rect(rect.x_center, rect.y_center, long_side / width * scale,
                long_side / height * scale, rect.rotation)


def _rect_iou(a, b):
    ax0, ay0 = a.x_center - a.width / 2, a.y_center - a.height / 2
    bx0, by0 = b.x_center - b.width / 2, b.y_center - b.height / 2
    ix = max(0.0, min(ax0 + a.width, bx0 + b.width) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + a.height, by0 + b.height) - max(ay0, by0))
    inter = ix * iy
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union > 0 else 0.0


def associate(rects, threshold):
    """AssociationNormRectCalculator over one list: a later rect replaces earlier overlapping ones."""
    result = []
    for rect in rects:
        if result:
            result = [kept for kept in result if not _rect_iou(rect, kept) > threshold]
        result.append(rect)
    return result


# ── Landmarks ────────────────────────────────────────────────────────────────
def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def decode_landmarks(raw, size=LANDMARK_SIZE):
    """TensorsToLandmarksCalculator (normalize=False, sigmoid visibility/presence): (39, 5) array."""
    values = np.asarray(raw, np.float32).reshape(MODEL_LANDMARKS, 5).astype(np.float64)
    out = np.empty_like(values)
    out[:, 0] = values[:, 0] / size
    out[:, 1] = values[:, 1] / size
    out[:, 2] = values[:, 2] / size
    out[:, 3] = _sigmoid(values[:, 3])
    out[:, 4] = _sigmoid(values[:, 4])
    return out


def refine_from_heatmap(landmarks, heatmap, kernel=HEATMAP_KERNEL, min_confidence=MIN_CONFIDENCE_TO_REFINE):
    """RefineLandmarksFromHeatmapCalculator: confidence-weighted x/y in a 7x7 window, clipped
    at the heatmap's edges (in place). MediaPipe weights by cell index (no +0.5)."""
    hm_h, hm_w, channels = heatmap.shape
    count = min(channels, len(landmarks))
    offset = (kernel - 1) // 2
    # C++ float->int truncates toward zero (so -0.5 cells still count as cell 0).
    cols = np.trunc(landmarks[:count, 0] * hm_w).astype(np.int64)
    rows = np.trunc(landmarks[:count, 1] * hm_h).astype(np.int64)
    inside = (cols >= 0) & (cols < hm_w) & (rows >= 0) & (rows < hm_h)
    steps = np.arange(-offset, offset + 1)
    r = rows[:, None] + steps                          # (count, kernel)
    c = cols[:, None] + steps
    valid = ((r >= 0) & (r < hm_h))[:, :, None] & ((c >= 0) & (c < hm_w))[:, None, :]
    values = heatmap[np.clip(r, 0, hm_h - 1)[:, :, None], np.clip(c, 0, hm_w - 1)[:, None, :],
                     np.arange(count)[:, None, None]].astype(np.float64)
    confidence = np.where(valid, _sigmoid(values), 0.0)
    total = confidence.sum(axis=(1, 2))
    refine = inside & (confidence.max(axis=(1, 2)) >= min_confidence) & (total > 0)
    if refine.any():
        safe = np.where(total > 0, total, 1.0)
        x = (confidence * c[:, None, :]).sum(axis=(1, 2)) / hm_w / safe
        y = (confidence * r[:, :, None]).sum(axis=(1, 2)) / hm_h / safe
        landmarks[:count, 0] = np.where(refine, x, landmarks[:count, 0])
        landmarks[:count, 1] = np.where(refine, y, landmarks[:count, 1])
    return landmarks


def project(landmarks, rect):
    """LandmarkProjectionCalculator with NORM_RECT only (the formula MediaPipe uses here)."""
    out = landmarks.copy()
    x = landmarks[:, 0] - 0.5
    y = landmarks[:, 1] - 0.5
    c, s = math.cos(rect.rotation), math.sin(rect.rotation)
    out[:, 0] = (c * x - s * y) * rect.width + rect.x_center
    out[:, 1] = (s * x + c * y) * rect.height + rect.y_center
    out[:, 2] = landmarks[:, 2] * rect.width
    return out


# ── Smoothing (VIDEO mode) ───────────────────────────────────────────────────
class LowPass:
    """mediapipe LowPassFilter over an array of independent values."""

    def __init__(self):
        self.initialized = False
        self.raw = None
        self.stored = None

    def apply(self, value, alpha):
        value = np.asarray(value, np.float64)
        result = value.copy() if not self.initialized else alpha * value + (1.0 - alpha) * self.stored
        self.initialized = True
        self.raw, self.stored = value, result
        return result


class OneEuro:
    """mediapipe OneEuroFilter (kUninitializedTimestamp variant), vectorised over values."""

    def __init__(self, min_cutoff, beta, derivate_cutoff, frequency=30.0):
        self.min_cutoff, self.beta, self.derivate_cutoff = min_cutoff, beta, derivate_cutoff
        self.frequency = frequency
        self.last_time = -1
        self.x = LowPass()
        self.dx = LowPass()

    def _alpha(self, cutoff):
        te = 1.0 / self.frequency
        tau = 1.0 / (2 * math.pi * np.asarray(cutoff, np.float64))
        return 1.0 / (1.0 + tau / te)

    def apply(self, timestamp_ns, value, value_scale):
        value = np.asarray(value, np.float64)
        if self.last_time >= timestamp_ns:
            return value
        if self.last_time != 0 and timestamp_ns != 0:
            self.frequency = 1.0 / ((timestamp_ns - self.last_time) * 1e-9)
        self.last_time = timestamp_ns
        if self.x.initialized:
            dvalue = (value - self.x.raw) * value_scale * self.frequency
        else:
            dvalue = np.zeros_like(value)
        edvalue = self.dx.apply(dvalue, self._alpha(self.derivate_cutoff))
        cutoff = self.min_cutoff + self.beta * np.abs(edvalue)
        return self.x.apply(value, self._alpha(cutoff))


class LandmarkSmoother:
    """VisibilitySmoothingCalculator(0.1) then LandmarksSmoothingCalculator(one-euro), as
    MultiplePoseLandmarksDetectorGraph wires them for one pose in VIDEO mode.

    A frame without a pose resets the one-euro filter (its IMAGE_SIZE input still ticks)
    but not the visibility filter, whose only input then carries no packet."""

    def __init__(self, min_cutoff=ONE_EURO["min_cutoff"], beta=ONE_EURO["beta"],
                 derivate_cutoff=ONE_EURO["derivate_cutoff"], visibility_alpha=VISIBILITY_ALPHA):
        self.settings = (min_cutoff, beta, derivate_cutoff)
        self.visibility_alpha = visibility_alpha
        self.visibility = LowPass()
        self.position = None

    def reset_position(self):
        self.position = None

    def apply(self, landmarks, timestamp_us, width, height, scale_rect):
        out = landmarks.copy()
        out[:, 3] = self.visibility.apply(landmarks[:, 3], self.visibility_alpha)
        if self.position is None:
            self.position = OneEuro(*self.settings)
        object_scale = (scale_rect.width * width + scale_rect.height * height) / 2.0
        if object_scale < 1e-6:
            return out
        pixels = out[:, :3] * (width, height, width)
        filtered = self.position.apply(int(timestamp_us) * 1000, pixels, 1.0 / object_scale)
        out[:, :3] = filtered / (width, height, width)
        return out


# ── The pipeline ─────────────────────────────────────────────────────────────
class PosePipeline:
    """detect(frame_bgr, timestamp_ms) -> 33 NormalizedLandmark or None, like
    PoseLandmarker.detect_for_video with num_poses=1 and the given confidences.

    `engine` provides detector(rgb_uint8_224) -> (boxes (2254, 12), scores (2254, 1))
    and landmarks(rgb_uint8_256) -> (raw (195,), pose_flag float, heatmap (64, 64, 39))."""

    def __init__(self, engine, min_detection_confidence=0.5, min_presence_confidence=0.5,
                 min_tracking_confidence=0.5, smooth=True):
        self.engine = engine
        self.min_detection_confidence = min_detection_confidence
        self.min_presence_confidence = min_presence_confidence
        self.min_tracking_confidence = min_tracking_confidence
        self.smoother = LandmarkSmoother() if smooth else None
        self.anchors = ssd_anchors()
        self.tracking_rect = None        # ROI from the previous frame's auxiliary landmarks
        self.last_timestamp_ms = None
        self.last_rect = None            # ROI used on the last frame (for diagnostics)
        self.detector_ran = False

    def reset(self):
        self.tracking_rect = None
        self.last_timestamp_ms = None
        if self.smoother is not None:
            self.smoother = LandmarkSmoother()

    def detect_rect(self, frame_bgr):
        """Run the detector; the first associated, expanded pose ROI or None."""
        height, width = frame_bgr.shape[:2]
        roi, padding = detector_roi(width, height)
        crop = warp(frame_bgr, roi, DETECTOR_SIZE, cv2.BORDER_CONSTANT)
        boxes, scores = self.engine.detector(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        detections = weighted_nms(decode_detections(boxes, scores, self.anchors, self.min_detection_confidence))
        rects = []
        for detection in detections:
            detection = remove_letterbox(detection, padding)
            rect = alignment_rect(detection.keypoints[0], detection.keypoints[1], width, height)
            rects.append(transform_rect(rect, width, height))
        rects = associate(rects, self.min_tracking_confidence)
        return rects[0] if rects else None

    def landmarks_in(self, frame_bgr, rect):
        """Landmark model on one ROI: ((39, 5) image-space landmarks, pose flag) or (None, flag)."""
        height, width = frame_bgr.shape[:2]
        roi = (rect.x_center * width, rect.y_center * height, rect.width * width,
               rect.height * height, rect.rotation)
        if not (roi[2] > 0 and roi[3] > 0):
            return None, 0.0
        crop = warp(frame_bgr, roi, LANDMARK_SIZE, cv2.BORDER_REPLICATE)
        raw, flag, heatmap = self.engine.landmarks(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        flag = float(np.asarray(flag).reshape(-1)[0])
        if not flag > self.min_presence_confidence:
            return None, flag
        landmarks = decode_landmarks(raw)
        refine_from_heatmap(landmarks, np.asarray(heatmap, np.float32).reshape(64, 64, MODEL_LANDMARKS))
        return project(landmarks, rect), flag

    def detect(self, frame_bgr, timestamp_ms):
        timestamp_ms = int(timestamp_ms)
        if self.last_timestamp_ms is not None and timestamp_ms <= self.last_timestamp_ms:
            raise ValueError("Input timestamp must be monotonically increasing.")
        self.last_timestamp_ms = timestamp_ms
        height, width = frame_bgr.shape[:2]

        rect = self.tracking_rect
        self.detector_ran = rect is None
        if rect is None:
            rect = self.detect_rect(frame_bgr)
        self.last_rect = rect
        self.tracking_rect = None
        landmarks = None
        if rect is not None:
            landmarks, _ = self.landmarks_in(frame_bgr, rect)
        if landmarks is None:
            if self.smoother is not None:
                self.smoother.reset_position()
            return None

        aux = landmarks[NUM_LANDMARKS:NUM_LANDMARKS + 2]
        next_rect = transform_rect(alignment_rect(aux[0], aux[1], width, height), width, height)
        self.tracking_rect = next_rect
        pose = landmarks[:NUM_LANDMARKS]
        if self.smoother is not None:
            pose = self.smoother.apply(pose, timestamp_ms * 1000, width, height, next_rect)
        return [NormalizedLandmark(float(x), float(y), float(z), float(v), float(p))
                for x, y, z, v, p in pose]
