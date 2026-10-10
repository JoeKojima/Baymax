"""
End-to-end scenario for Ember's identity on realtime_gemini_10 (hardware and
Gemini faked): the system instruction order and content, capabilities that
follow device.toml / memory state, [SELF] status, and a family profile edit
passed to Ember mid-session only when it's quiet. Prints PASS on success.
"""
import asyncio, importlib, os, sys, tempfile, time
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
BAYMAX = os.path.join(HERE, "..", "Baymax-main")
sys.path.insert(0, BAYMAX); sys.path.insert(0, HERE)
for name in ["cv2", "mediapipe", "mediapipe.tasks", "mediapipe.tasks.python",
             "mediapipe.tasks.python.vision", "semantic_embedder"]:
    sys.modules[name] = mock.MagicMock(name=name)
sd = mock.MagicMock(name="sounddevice")
sd.query_devices.return_value = [{"name": "pipewire", "max_output_channels": 0, "max_input_channels": 2},
                                 {"name": "default", "max_output_channels": 2, "max_input_channels": 0}]
sys.modules["sounddevice"] = sd
os.environ.pop("BAYMAX_DEVICE_KEY", None)

import ember_self
tmp = tempfile.mkdtemp()
ember_self.PROFILE_PATH = os.path.join(tmp, "ember_profile.json")
ember_self.save_profile({"user_name": "Margaret", "about": "Loves her garden.",
                         "household": [{"name": "Sarah", "relationship": "daughter"}]})
with mock.patch("subprocess.run"), mock.patch("subprocess.check_output", return_value=b""):
    R = importlib.import_module("realtime_gemini_10")
from google.genai import types

# 1. System instruction: identity first, then memories, then conversation rules
mem = mock.MagicMock()
mem._collection.count.return_value = 2
mem._collection.get.return_value = {"documents": ["Margaret grew up in Ohio.", "She has a cat."]}
R._memory_embedder = mem
cfg = types.LiveConnectConfig(**R._build_config())
si = cfg.system_instruction
assert si.startswith("# Who you are\nYou are Ember, a companion robot for older adults. You live in the home of Margaret"), si[:200]
i_id, i_mem, i_rules, i_fall = (si.index("# Who you are"), si.index("What you remember from earlier conversations"),
                                si.index("# How you talk"), si.index("[FALL ALERT]"))
assert i_id < i_mem < i_rules < i_fall, (i_id, i_mem, i_rules, i_fall)
assert "- Sarah — daughter" in si and "From their family: Loves her garden." in si
assert "newest first" in si and "\n- Margaret grew up in Ohio.\n- She has a cat.\n" in si, si[si.index("What you remember"):][:300]
assert "Remember what they tell you from one conversation to the next." in si
fall_line = "Watch for falls with your camera."
assert (fall_line in si) == R.FALL_DETECTION_ENABLED
print(f"system instruction ok ({len(si)} chars); fall detection enabled={R.FALL_DETECTION_ENABLED}")

# Fall detection off in device.toml → honest limitation instead
with mock.patch.object(R, "FALL_DETECTION_ENABLED", False):
    si_off = R._build_config()["system_instruction"]
assert fall_line not in si_off and "Automatic fall detection is turned off" in si_off
R._memory_embedder = None
si_nomem = R._build_config()["system_instruction"]
assert "Your memory of past conversations isn't working right now" in si_nomem
print("capabilities follow device.toml / memory state: ok")

# 2. self_awareness_monitor against a fake session
R._self_state = ember_self.SelfState(debounce=0.3, min_interval=0.5, refresh_interval=3600)
sent = []
class FakeSession:
    async def send_client_content(self, *, turns, turn_complete):
        assert turn_complete is False
        sent.append(turns["parts"][0]["text"])

async def wait_for(pred, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred(): return True
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out; sent={sent}")

async def main():
    with R._initiation_ts_lock:
        R._last_loud_mic_ts = 0.0
    task = asyncio.create_task(R.self_awareness_monitor(FakeSession()))
    await wait_for(lambda: sent)
    print("session start ->", sent[0])
    assert sent[0].startswith("[SELF] It's ") and "Nobody is in front of your camera" in sent[0]
    assert "haven't heard from Margaret since you started up" in sent[0]

    # A face appears and stays → debounced update
    for _ in range(30):
        with R._face_state_lock:
            R._face_last_seen_ts = time.monotonic()
        await asyncio.sleep(0.05)
        if len(sent) > 1: break
    await wait_for(lambda: len(sent) >= 2)
    print("face in view ->", sent[1])
    assert "Someone is in front of your camera right now." in sent[1]

    # Not sent while Ember is talking (gate), sent once quiet
    with R._playback_lock:
        R._playback_buffer = b"\x00" * 100
    await asyncio.sleep(0.05)
    os.utime(ember_self.PROFILE_PATH, None)                       # touch only: no content change
    ember_self.save_profile({"user_name": "Maggie", "about": "Loves her garden.",
                             "household": [{"name": "Sarah", "relationship": "daughter"},
                                           {"name": "Milo", "relationship": "her cat"}]})
    await asyncio.sleep(1.5)
    assert not any("[PROFILE UPDATE]" in m for m in sent), "spoke over Ember"
    with R._playback_lock:
        R._playback_buffer = b""
    await wait_for(lambda: any("[PROFILE UPDATE]" in m for m in sent))
    update = next(m for m in sent if "[PROFILE UPDATE]" in m)
    print("profile edit ->", update.replace("\n", " | "))
    assert "Their name is Maggie." in update and "- Milo — her cat" in update
    assert R._profile["user_name"] == "Maggie"
    n = len(sent)
    await asyncio.sleep(1.0)
    assert not any("[PROFILE UPDATE]" in m for m in sent[n:]), "profile update repeated"
    task.cancel()
    print("PASS")

asyncio.run(main())
