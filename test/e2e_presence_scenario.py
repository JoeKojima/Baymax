"""
End-to-end check of "someone is in front of you" with the REAL MediaPipe pose
model: v10's own camera thread, fed by a fake webcam, must mark someone in view
for a person and not for an empty room. The dashboard's live picture is
drawn only while someone watches it (or when the frame has no time to spare,
so the loop's timing never changes). Needs mediapipe and
EMBER_FACE_TEST_DATA (for a photo of a person). Prints PASS on success.
"""
import importlib, os, socket, sys, threading, time, urllib.request
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
        time.sleep(scene.get("wait", 1 / 15))
        scene["reads"] = scene.get("reads", 0) + 1
        if scene.get("stopped"):
            return False, None
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

    # Live picture: drawn only for a viewer (or when the frame has no spare time)
    draws = [0]
    real_draw = R._draw_annotated
    def counting_draw(*a):
        draws[0] += 1
        return real_draw(*a)
    R._draw_annotated = counting_draw
    scene.update(frame=person_frame(), wait=0.0)              # frames ready, like a 30 fps camera
    R.DRAW_SKIP_MIN_SPARE_S = -1.0                            # every frame counts as having spare time
    def counted(seconds):
        """(pictures drawn, frames read) over the next `seconds`."""
        draws[0], reads = 0, scene.get("reads", 0)
        time.sleep(seconds)
        return draws[0], scene.get("reads", 0) - reads
    time.sleep(1.0)
    unwatched, _ = counted(1.5)
    with R._annotated_frame_lock: idle_picture = R._annotated_frame
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    threading.Thread(target=R._start_mjpeg_server, args=(port,), daemon=True).start()
    time.sleep(0.5)
    def first_picture():
        stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=10)
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            byte = stream.read(1)
            assert byte, "live-view stream closed before a picture"
            header += byte
        length = int(header.split(b"Content-Length: ")[1].split(b"\r\n")[0])
        return stream, cv2.imdecode(np.frombuffer(stream.read(length), np.uint8), cv2.IMREAD_COLOR)
    # The camera stops while nobody watches: a viewer still gets its last frame
    scene["stopped"] = True; time.sleep(0.5)
    stream, stopped_picture = first_picture()
    stream.close(); time.sleep(0.5)
    scene["stopped"] = False; time.sleep(0.5)
    stream, picture = first_picture()
    with R._annotated_frame_lock: viewers = R._mjpeg_viewers
    watched, watched_reads = counted(1.0)
    stream.close()
    time.sleep(1.5)
    with R._annotated_frame_lock: viewers_after = R._mjpeg_viewers
    after, _ = counted(1.0)
    R.DRAW_SKIP_MIN_SPARE_S = 1.0                             # no frame has a second to spare
    time.sleep(0.5)
    no_spare, no_spare_reads = counted(1.0)
    print(f"live picture: unwatched {unwatched} draws; camera stopped -> "
          f"{None if stopped_picture is None else stopped_picture.shape}; watched {watched} of {watched_reads} "
          f"frames ({viewers} viewer); after the viewer left {after} ({viewers_after} viewers); "
          f"no spare time {no_spare} of {no_spare_reads}; picture {None if picture is None else picture.shape}")
    R._shutdown_event.set(); t.join(5)

assert seen_empty == 0.0, "empty room marked as someone in view"
assert age is not None and age < 1.0, "person not detected by real pose model"
assert shared is not None and shared.shape == (480, 640, 3)
assert "Someone is in front of your camera right now." in snap
assert "Nobody is in front of your camera right now." in snap2
assert unwatched == 0 and idle_picture is None, "drew the live picture with nobody watching"
assert viewers == 1 and watched_reads >= 2 and watched >= watched_reads - 1, "not drawn while watched"
raw = person_frame()[10:100, 10:300].astype(int)
for shown in (stopped_picture, picture):
    assert shown is not None and shown.shape == (480, 640, 3)
    assert np.abs(shown[10:100, 10:300].astype(int) - raw).mean() > 20, "the streamed picture has no overlay"
assert viewers_after == 0 and after == 0, "kept drawing after the viewer left"
assert no_spare_reads >= 2 and no_spare >= no_spare_reads - 1, \
    "skipped drawing on frames without spare time (would change the loop's timing)"
print("PASS")
