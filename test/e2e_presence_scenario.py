"""
End-to-end check of "someone is in front of you" with the REAL MediaPipe pose
model: v10's own camera thread, fed by a fake webcam, must mark someone in view
for a person and not for an empty room. Needs mediapipe and
EMBER_FACE_TEST_DATA (for a photo of a person). Prints PASS on success.
"""
import importlib, os, sys, threading, time
from unittest import mock
import cv2, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
BAYMAX = os.path.join(HERE, "..", "Baymax-main")
sys.path.insert(0, BAYMAX); sys.path.insert(0, HERE)
sys.modules["semantic_embedder"] = mock.MagicMock()
sd = mock.MagicMock()
sd.query_devices.return_value = [{"name": "pipewire", "max_output_channels": 0, "max_input_channels": 2},
                                 {"name": "default", "max_output_channels": 2, "max_input_channels": 0}]
sys.modules["sounddevice"] = sd
with mock.patch("subprocess.run"), mock.patch("subprocess.check_output", return_value=b""):
    R = importlib.import_module("realtime_gemini_10")
import test_face_models as T
T.DATA = os.environ["EMBER_FACE_TEST_DATA"]
R._ensure_pose_model()

person = T._people(12)[0][1][0]
def person_frame():
    # head-and-shoulders, as when sitting ~1 m in front of the robot
    img = cv2.resize(cv2.imread(person), (420, 420))
    f = np.full((480, 640, 3), 110, np.uint8); f[30:450, 110:530] = img
    return f
empty = np.full((480, 640, 3), 110, np.uint8)
scene = {"frame": empty}
class FakeCapture:
    def __init__(self, *a): pass
    def isOpened(self): return True
    def set(self, *a): return True
    def read(self):
        time.sleep(1 / 15)
        return True, scene["frame"].copy()
    def release(self): pass

with mock.patch.object(R.cv2, "VideoCapture", FakeCapture), \
     mock.patch.object(R, "_notify_webapp", lambda *a, **k: None):
    t = threading.Thread(target=R._fall_detection_thread, daemon=True); t.start()
    time.sleep(4)
    with R._face_state_lock: seen_empty = R._face_last_seen_ts
    print("empty room -> face_last_seen_ts:", seen_empty)
    scene["frame"] = person_frame()
    time.sleep(4)
    with R._face_state_lock: seen = R._face_last_seen_ts
    age = time.monotonic() - seen if seen else None
    print("person -> last seen", None if age is None else f"{age:.2f}s ago")
    with R._latest_frame_lock: shared = R._latest_frame
    print("shared frame for Gemini/face-id:", None if shared is None else shared.shape)
    R._update_self_facts()
    snap = R._self_state.snapshot()
    print("[SELF] while person present:", snap)
    scene["frame"] = empty
    time.sleep(R.SELF_IN_VIEW_SECONDS + 1.5)
    R._update_self_facts()
    snap2 = R._self_state.snapshot()
    print("[SELF] after they left:", snap2)
    R._shutdown_event.set(); t.join(5)

assert seen_empty == 0.0, "empty room marked as someone in view"
assert age is not None and age < 1.0, "person not detected by real pose model"
assert shared is not None and shared.shape == (480, 640, 3)
assert "Someone is in front of your camera right now." in snap
assert "Nobody is in front of your camera right now." in snap2
print("PASS")
