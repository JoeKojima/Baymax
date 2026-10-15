"""
record.py — record pose landmarks through a scripted session, for tuning the
fall detector on a real robot.

Runs only the camera and MediaPipe Pose (same settings as the AI core), speaks
each instruction through the robot's speaker (spd-say), and writes one JSON
line per frame: time, the current instruction's label, all 33 landmarks and
what the current FallDetector decided. No images are saved.

    python -m fall_detection.record OUT.jsonl            # from Baymax-main
    python -m fall_detection.replay OUT.jsonl            # score detector rules

The AI core must be stopped first (it holds the camera).
"""
import json
import subprocess
import sys
import time

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision as mp_vision

from fall_detection.detector import FallDetector

FPS = 15
POSE_MODEL = "pose_landmarker_full.task"

# (label, seconds, spoken instruction). Labels starting with "fall" are the
# only ones where a detection is correct.
SCRIPT = [
    ("setup",      20, "Fall detection recording. Get into position in front of the robot. "
                       "Starting in twenty seconds."),
    ("stand",      15, "Stand still in front of the robot."),
    ("sit_down",   10, "Now sit down in your chair."),
    ("seated",     40, "Stay seated. Talk, move your hands and shift around, like a normal conversation."),
    ("lean_fwd",   25, "Lean forward toward the robot, then sit back. Repeat a few times."),
    ("lean_side",  20, "Lean back in your chair, then to each side."),
    ("stand_up",   10, "Stand up."),
    ("bend",       20, "Bend down as if picking something up off the floor, then stand up. Do it twice."),
    ("sit_stand",  15, "Sit down, then stand back up."),
    ("fall_slow",  25, "Now lie down on the floor where the robot can see you, and stay down."),
    ("get_up",     15, "Get up and stand."),
    ("fall_fast",  25, "Only if you feel safe: go down to the floor quickly, onto something soft, "
                       "and stay down. Otherwise just stand still."),
    ("get_up2",    15, "Get up. Recording finished in fifteen seconds."),
]


def say(text):
    subprocess.Popen(["spd-say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_camera(indices=(0, 1, 2)):
    for i in indices:
        cap = cv2.VideoCapture(i)
        if cap.isOpened() and cap.read()[0]:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            return cap
        cap.release()
    sys.exit("No camera found (is the AI core still running?)")


def main(out_path):
    cap = open_camera()
    options = mp_vision.PoseLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=POSE_MODEL),
        running_mode=mp_vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    detector = FallDetector(stay_down_s=2.0)
    start = time.monotonic()
    frames = detections = 0
    with mp_vision.PoseLandmarker.create_from_options(options) as landmarker, \
            open(out_path, "w") as out:
        for label, seconds, text in SCRIPT:
            print(f"[{time.monotonic() - start:6.1f}s] {label}: {text}", flush=True)
            say(text)
            seg_end = time.monotonic() + seconds
            while time.monotonic() < seg_end:
                t0 = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res = landmarker.detect_for_video(image, int((t0 - start) * 1000))
                lms = res.pose_landmarks[0] if res.pose_landmarks else None
                d = detector.update(lms)
                if d["fall_detected"]:
                    detections += 1
                    print(f"[{t0 - start:6.1f}s]   current detector: FALL during '{label}'", flush=True)
                out.write(json.dumps({
                    "t": round(t0 - start, 4),
                    "label": label,
                    "lm": [[round(p.x, 4), round(p.y, 4), round(p.z, 4), round(p.visibility, 3)]
                           for p in lms] if lms else None,
                    "fall": d["fall_detected"],
                }) + "\n")
                frames += 1
                time.sleep(max(0.0, 1.0 / FPS - (time.monotonic() - t0)))
    cap.release()
    say("Recording finished. Thank you.")
    print(f"{frames} frames, {detections} detections by the current rules -> {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
