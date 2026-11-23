"""Pure fall decision: landmarks + timestamps in, decision out. No camera or MediaPipe.

`FallDecision` wraps the packaged upstream `fall_detection.detector.FallDetector`
(left byte-for-byte unchanged; its hash is checked) and adds:
- an explicit `now` timestamp, so recorded/synthetic sequences replay exactly;
- a coarse posture label for the demo screen;
- the countdown until a pending fall is confirmed.
The decision rules are the upstream ones; tests check both paths agree frame by frame.
"""
import math
import time

from fall_detection.detector import FallDetector, _IDX, _MIN_VISIBILITY

# Thresholds the v8 runtime passes to the worker (realtime_gemini_8.py FALL_* constants).
# A test keeps these in sync; replay uses them when the live worker has not started.
DEFAULT_SETTINGS = dict(angle_threshold=45.0, ang_vel_threshold=25.0, hip_vel_threshold=0.12,
                        history_window=8, confirmation_frames=2, cooldown_seconds=3.0)

POSTURES = ("not visible", "standing", "upright", "sitting", "bending", "lying",
            "going down (possible fall)", "on floor (possible fall)")


class ClockedFallDetector(FallDetector):
    """Upstream detector whose update() accepts the frame time instead of reading the clock."""

    def update(self, landmarks, now=None):
        # Mirrors FallDetector.update with `now` injectable; _observe already takes `now`.
        now = time.monotonic() if now is None else now
        result = dict(fall_detected=False, fall_active=self.fall_active, fall_pending=False,
                      trunk_angle=None, angular_vel=None, hip_descent_vel=None)
        self._observe(landmarks, now, result)
        if self._pending_since is not None:
            angle = result["trunk_angle"]
            if angle is not None and angle < self.upright_angle:
                self._pending_since = None
            elif now - self._pending_since >= self.stay_down_s:
                self._pending_since = None
                self._cooldown_until = now + self.cooldown_seconds
                self.fall_active = True
                result["fall_detected"] = True
                result["fall_active"] = True
                result.update(self._pending_motion)
        result["fall_pending"] = self._pending_since is not None
        return result

    def countdown(self, now):
        """Seconds until a pending fall is confirmed, or None when nothing is pending."""
        if self._pending_since is None:
            return None
        return max(0.0, self.stay_down_s - (now - self._pending_since))


def _visible(points, *names):
    return all(points[_IDX[name]].visibility >= _MIN_VISIBILITY for name in names)


def _mid(points, a, b):
    return ((points[a].x + points[b].x) / 2, (points[a].y + points[b].y) / 2)


def posture(landmarks, trunk_angle=None):
    """Coarse label from one frame: not visible / standing / upright / sitting / bending / lying."""
    if landmarks is None or trunk_angle is None or not _visible(
            landmarks, "left_shoulder", "right_shoulder", "left_hip", "right_hip"):
        return "not visible"
    if trunk_angle >= 60:
        return "lying"
    if trunk_angle >= 30:
        return "bending"
    knees = (25, 26)
    if all(landmarks[i].visibility >= _MIN_VISIBILITY for i in knees):
        hip, knee = _mid(landmarks, 23, 24), _mid(landmarks, *knees)
        thigh = math.degrees(math.atan2(abs(knee[0] - hip[0]), abs(knee[1] - hip[1])))
        return "sitting" if thigh > 50 else "standing"
    return "upright"


class FallDecision:
    """One person's decision state. step(landmarks, now) -> dict; also usable as worker.update()."""

    def __init__(self, **settings):
        self.settings = dict(DEFAULT_SETTINGS, **settings)
        self.detector = ClockedFallDetector(**self.settings)
        self._down_after_fall = False

    @property
    def stay_down_s(self):
        return self.detector.stay_down_s

    def update(self, landmarks, now=None):
        now = time.monotonic() if now is None else now
        result = self.detector.update(landmarks, now)
        angle = None if result["fall_detected"] else result["trunk_angle"]
        label = posture(landmarks, angle if angle is not None else
                        (self.detector._angles[-1] if result["fall_detected"] and self.detector._angles else None))
        if result["fall_detected"]:
            self._down_after_fall = True
        elif angle is not None and angle < self.detector.upright_angle:
            self._down_after_fall = False
        if result["fall_pending"]:
            label = "going down (possible fall)"
        elif self._down_after_fall and label in ("lying", "bending", "not visible"):
            label = "on floor (possible fall)"
        result.update(posture=label, countdown_s=self.detector.countdown(now), time=now)
        return result

    step = update

    def reset(self):
        self.detector.reset()
        self._down_after_fall = False


def replay(frames, settings=None, start=0.0):
    """Run [(t, landmarks)] through a fresh FallDecision. Returns one result dict per frame."""
    decision = FallDecision(**(settings or {}))
    return [decision.update(landmarks, start + moment) for moment, landmarks in frames]


def detections(frames, settings=None):
    """Times (seconds from the sequence start) at which a fall was confirmed."""
    return [frames[i][0] for i, result in enumerate(replay(frames, settings)) if result["fall_detected"]]


def points(landmarks, digits=3):
    """Compact [[x, y, visibility], ...] for the local demo screen (None when no pose)."""
    if landmarks is None:
        return None
    return [[round(p.x, digits), round(p.y, digits), round(getattr(p, "visibility", 1.0), 2)] for p in landmarks]
