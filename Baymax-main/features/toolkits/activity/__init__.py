"""Activity tracking, Tier 1 (shadow mode): what the person is doing, from pose.

v10's camera loop already runs MediaPipe Pose for fall detection; its results
reach this module through `fall_detection.pose_observers`, so there is no new
camera stream and no new model. A worker thread classifies about five samples a
second (posture.py), turns them into segments (tracker.py) and keeps them in a
local SQLite file (store.py). devUI's monitor page shows the day's timeline.

Off unless BAYMAX_ACTIVITY_ENABLED=1. Nothing leaves the robot in this version.
"""
import atexit
import os
import threading
import time

from . import posture
from .store import Store
from .tracker import GAP_S, TALK_RECENT_S, Tracker

SAMPLE_INTERVAL = 0.2   # Five samples per second is plenty for minutes-long activities.
FLUSH_S = 30.0          # The open segment is saved this often, so a crash loses at most this.
PRUNE_S = 6 * 3600.0


def enabled():
    return os.getenv("BAYMAX_ACTIVITY_ENABLED", "0") == "1"


def _record(message):
    try:
        import runtime_monitor
        runtime_monitor.record("activity", message)
    except Exception:
        pass


class Activity:
    def __init__(self, store_factory=Store, clock=time.time, enabled_check=enabled):
        self.store_factory = store_factory
        self.clock = clock
        self.enabled_check = enabled_check
        self.is_enabled = None        # Decided on the first pose sample (after .env is loaded).
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.pending = None
        self.last_accepted = 0.0
        self.last_user_speech = None
        self.state = "Waiting for pose"
        self.samples = 0

    # Camera thread: must stay cheap.
    def observe(self, landmarks, world_landmarks=None, frame_shape=None):
        if self.is_enabled is None:
            self.is_enabled = self.enabled_check()
            if not self.is_enabled:
                self.state = "Disabled"
        if not self.is_enabled or self.stop_event.is_set():
            return
        now = self.clock()
        if now - self.last_accepted < SAMPLE_INTERVAL:
            return
        self.last_accepted = now
        image = None if landmarks is None else tuple((p.x, p.y, p.z, p.visibility) for p in landmarks)
        world = None if world_landmarks is None else tuple((p.x, p.y, p.z) for p in world_landmarks)
        aspect = frame_shape[1] / frame_shape[0] if frame_shape and frame_shape[0] else 4 / 3
        with self.lock:
            self.pending = (now, image, world, aspect)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="ember-activity", daemon=True)
                self.thread.start()
        self.wake.set()

    # Transcript listener (event loop): note when the person last spoke.
    def heard(self, speaker, message):
        if speaker == "user" and (message.get("text") or "").strip():
            self.last_user_speech = self.clock()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _run(self):
        try:
            store = self.store_factory()
            store.recover()
            store.prune(self.clock())
        except Exception as error:
            self.state = f"Unavailable: {error}"
            self.is_enabled = False
            print(f"[ACTIVITY] Store unavailable ({error}); activity tracking off.", flush=True)
            _record(self.state)
            return
        tracker = Tracker(on_close=lambda segment: store.save(segment))
        print(f"[ACTIVITY] Shadow mode: posture segments kept on this robot only ({store.path.name}).", flush=True)
        self.state = "Running"
        previous = previous_t = None
        last_flush = last_prune = self.clock()
        try:
            while not self.stop_event.is_set():
                self.wake.wait(1.0)
                self.wake.clear()
                with self.lock:
                    item, self.pending = self.pending, None
                now = self.clock()
                if item is None:
                    if tracker.last_t is not None and now - tracker.last_t > GAP_S:
                        tracker.gap()  # Camera off or pose stopped: close the segment now.
                        previous = previous_t = None
                    continue
                t, image, world, aspect = item
                feature = posture.features(image, world, aspect) if image is not None else None
                label = "absent" if image is None else posture.classify(feature)
                body, _ = (posture.movement(previous, feature, t - previous_t)
                           if previous_t is not None else (None, None))
                previous, previous_t = (feature, t) if feature is not None else (None, None)
                talking = self.last_user_speech is not None and t - self.last_user_speech < TALK_RECENT_S
                tracker.sample(t, label, body,
                               hip=feature["hip"] if feature else None,
                               torso=feature["torso"] if feature else None,
                               central=feature["central"] if feature else None,
                               hand_mouth=feature["hand_mouth"] if feature else None,
                               talking=talking)
                self.samples += 1
                if now - last_flush >= FLUSH_S:
                    last_flush = now
                    current = tracker.open_segment()
                    if current:
                        store.save(current, is_open=True)
                if now - last_prune >= PRUNE_S:
                    last_prune = now
                    store.prune(now)
        except Exception as error:
            self.state = f"Stopped after an error: {error}"
            print(f"[ACTIVITY] {self.state}", flush=True)
            _record(self.state)
            self.is_enabled = False
        finally:
            try:
                tracker.gap()
            except Exception:
                pass
            store.close()


activity = Activity()


def register():
    from ..fall_detection import pose_observers
    import runtime_monitor
    if activity.observe not in pose_observers:
        pose_observers.append(activity.observe)
    if activity.heard not in runtime_monitor.transcript_listeners:
        runtime_monitor.transcript_listeners.append(activity.heard)
        atexit.register(activity.stop)  # A normal exit (Ctrl+C) saves the open segment.


register()
