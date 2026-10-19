"""
main.py — Live fall detection using MediaPipe Pose (Tasks API)

Compatible with mediapipe >= 0.10.30.
On first run the pose landmarker model (~12 MB) is downloaded automatically.

Usage:
    python main.py                          # default webcam (index 0)
    python main.py --source 1               # webcam index 1
    python main.py --source path/to/vid.mp4 # video file

Keyboard shortcuts while running:
    Q — quit
    R — reset detector state
    D — toggle debug angle graph
"""

import argparse
import os
import time
import urllib.request
from collections import deque

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision as mp_vision
import numpy as np

from detector import FallDetector


# ── Model ──────────────────────────────────────────────────────────────────────
_MODEL_FILENAME = "pose_landmarker_full.task"
_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task"
)


def _ensure_model():
    if not os.path.exists(_MODEL_FILENAME):
        print(f"Downloading pose landmarker model → {_MODEL_FILENAME}  (~12 MB) ...")
        urllib.request.urlretrieve(_MODEL_URL, _MODEL_FILENAME)
        print("Model downloaded successfully.")


# ── Pose skeleton connections (landmark index pairs) ───────────────────────────
# Subset of the 33-point MediaPipe skeleton — body-relevant connections only.
_POSE_CONNECTIONS = [
    # Face outline (minimal)
    (0, 1), (1, 2), (2, 3), (3, 7),
    (0, 4), (4, 5), (5, 6), (6, 8),
    # Shoulders & arms
    (11, 12), (11, 13), (13, 15),
    (12, 14), (14, 16),
    # Torso
    (11, 23), (12, 24), (23, 24),
    # Left leg
    (23, 25), (25, 27), (27, 29), (27, 31), (29, 31),
    # Right leg
    (24, 26), (26, 28), (28, 30), (28, 32), (30, 32),
]

# ── Colours (BGR) ──────────────────────────────────────────────────────────────
_WHITE  = (255, 255, 255)
_GREEN  = (0, 220, 0)
_YELLOW = (0, 200, 255)
_RED    = (30,  30, 220)
_DARK   = (20,  20,  20)
_CYAN   = (255, 220, 0)


# ── Drawing helpers ────────────────────────────────────────────────────────────

def _text(img, text, pos, scale=0.65, color=_WHITE, thickness=2):
    """Draw text with a dark drop-shadow for readability."""
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX,
                scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX,
                scale, color, thickness, cv2.LINE_AA)


def _draw_skeleton(frame, landmarks):
    """Draw the pose skeleton from a Tasks API landmark list."""
    if not landmarks:
        return
    h, w = frame.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]

    for a, b in _POSE_CONNECTIONS:
        if a < len(pts) and b < len(pts):
            if landmarks[a].visibility > 0.4 and landmarks[b].visibility > 0.4:
                cv2.line(frame, pts[a], pts[b], _GREEN, 2, cv2.LINE_AA)

    for i, (x, y) in enumerate(pts):
        if landmarks[i].visibility > 0.4:
            cv2.circle(frame, (x, y), 3, _WHITE, -1, cv2.LINE_AA)


def _draw_trunk_line(frame, landmarks):
    """Draw the shoulder-midpoint → hip-midpoint trunk axis."""
    if not landmarks:
        return
    h, w = frame.shape[:2]
    ls, rs = landmarks[11], landmarks[12]
    lh, rh = landmarks[23], landmarks[24]
    sm = (int((ls.x + rs.x) / 2 * w), int((ls.y + rs.y) / 2 * h))
    hm = (int((lh.x + rh.x) / 2 * w), int((lh.y + rh.y) / 2 * h))
    cv2.line(frame, hm, sm, _YELLOW, 3, cv2.LINE_AA)
    cv2.circle(frame, sm, 7, _YELLOW, -1, cv2.LINE_AA)
    cv2.circle(frame, hm, 7, _YELLOW, -1, cv2.LINE_AA)


def _draw_hud(frame, result: dict, fps: float):
    """Top-left metrics panel."""
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (310, 115), _DARK, -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    angle = result["trunk_angle"]
    ang_v = result["angular_vel"]
    hip_v = result["hip_descent_vel"]

    _text(frame, f"FPS:          {fps:5.1f}",                                         (10, 24),  color=_CYAN)
    _text(frame, f"Trunk angle:  {angle:.1f} deg" if angle is not None else "Trunk angle:  --", (10, 50))
    _text(frame, f"Angular vel:  {ang_v:.1f} deg/s" if ang_v is not None else "Angular vel:  --", (10, 76))
    _text(frame, f"Hip descent:  {hip_v:.2f} /s" if hip_v is not None else "Hip descent:  --", (10, 102))


def _draw_fall_alert(frame):
    """Full-frame red tint + centred FALL DETECTED banner."""
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), _RED, -1)
    cv2.addWeighted(overlay, 0.35, frame, 0.65, 0, frame)

    label = "FALL DETECTED"
    font, scale, thick = cv2.FONT_HERSHEY_DUPLEX, 2.0, 3
    (tw, th), _ = cv2.getTextSize(label, font, scale, thick)
    x, y = (w - tw) // 2, (h + th) // 2
    cv2.putText(frame, label, (x + 4, y + 4), font, scale, (0, 0, 0), thick + 4, cv2.LINE_AA)
    cv2.putText(frame, label, (x, y),          font, scale, _WHITE,    thick,     cv2.LINE_AA)


def _draw_angle_graph(frame, angle_history: deque, threshold: float):
    """Mini line chart of trunk angle history (bottom-right corner)."""
    if len(angle_history) < 2:
        return
    h, w = frame.shape[:2]
    gw, gh = 200, 80
    gx, gy = w - gw - 10, h - gh - 10

    overlay = frame.copy()
    cv2.rectangle(overlay, (gx, gy), (gx + gw, gy + gh), _DARK, -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

    # Threshold line
    ty = gy + gh - int(threshold / 90.0 * gh)
    cv2.line(frame, (gx, ty), (gx + gw, ty), _RED, 1)

    vals = list(angle_history)
    n = len(vals)
    for i in range(1, n):
        x0 = gx + int((i - 1) / (n - 1) * gw)
        x1 = gx + int(i       / (n - 1) * gw)
        y0 = gy + gh - int(min(vals[i - 1], 90) / 90.0 * gh)
        y1 = gy + gh - int(min(vals[i],     90) / 90.0 * gh)
        cv2.line(frame, (x0, y0), (x1, y1), _GREEN, 2, cv2.LINE_AA)

    _text(frame, "Angle (0-90°)", (gx + 4, gy + 12), scale=0.40, color=_CYAN, thickness=1)


# ── Main loop ──────────────────────────────────────────────────────────────────

def run(source):
    _ensure_model()

    # Build the Tasks API landmarker (VIDEO mode = synchronous, frame-by-frame)
    base_options = mp_tasks.BaseOptions(model_asset_path=_MODEL_FILENAME)
    options = mp_vision.PoseLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    fall_detector = FallDetector(
        angle_threshold=60.0,       # degrees — tune for camera angle / subject
        ang_vel_threshold=40.0,     # deg/s   — lower = more sensitive
        hip_vel_threshold=0.20,     # norm/s  — lower = more sensitive
        history_window=8,
        confirmation_frames=3,
        cooldown_seconds=3.0,
    )

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video source: {source!r}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    angle_history: deque = deque(maxlen=60)   # ~2 s at 30 fps
    fps_history:   deque = deque(maxlen=30)
    show_debug = True

    prev_t  = time.monotonic()
    start_t = prev_t                           # reference for timestamps

    print("Fall detection running.  Q = quit | R = reset | D = toggle debug graph")

    with mp_vision.PoseLandmarker.create_from_options(options) as landmarker:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            # ── Pose inference ────────────────────────────────────────
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

            # Timestamp must be strictly increasing integer milliseconds
            timestamp_ms = int((time.monotonic() - start_t) * 1000)
            detection = landmarker.detect_for_video(mp_image, timestamp_ms)

            landmarks = (
                detection.pose_landmarks[0]
                if detection.pose_landmarks else None
            )

            # ── Fall detection ────────────────────────────────────────
            result = fall_detector.update(landmarks)
            if result["trunk_angle"] is not None:
                angle_history.append(result["trunk_angle"])

            # ── FPS ───────────────────────────────────────────────────
            now = time.monotonic()
            fps_history.append(1.0 / max(now - prev_t, 1e-6))
            prev_t = now
            fps = float(np.mean(fps_history))

            # ── Render ────────────────────────────────────────────────
            _draw_skeleton(frame, landmarks)
            _draw_trunk_line(frame, landmarks)
            _draw_hud(frame, result, fps)

            if show_debug:
                _draw_angle_graph(frame, angle_history, fall_detector.angle_threshold)

            if result["fall_active"]:
                _draw_fall_alert(frame)

            status_color = _RED if result["fall_active"] else _GREEN
            _text(frame,
                  "Status: FALL" if result["fall_active"] else "Status: OK",
                  (10, frame.shape[0] - 12),
                  color=status_color)

            cv2.imshow("Fall Detection  [Q=quit  R=reset  D=debug]", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key == ord("r"):
                fall_detector.reset()
                angle_history.clear()
                print("Detector reset.")
            elif key == ord("d"):
                show_debug = not show_debug

    cap.release()
    cv2.destroyAllWindows()
    print("Stopped.")


# ── Entry point ────────────────────────────────────────────────────────────────

def _parse_source(raw: str):
    try:
        return int(raw)
    except ValueError:
        return raw


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live fall detection")
    parser.add_argument(
        "--source", default="0",
        help="Camera index (0, 1, …) or path to a video file. Default: 0"
    )
    args = parser.parse_args()
    run(_parse_source(args.source))