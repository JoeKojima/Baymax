"""
End-to-end check of Gemini session continuity in realtime_gemini_10.run()
against a fake Live server: compression is requested, resumption handles are
kept, a GoAway leads to an immediate resumed reconnect (no audio reset), a
handle that keeps failing is dropped, and an expired handle isn't used.
Also: a reply cut off by the old connection doesn't leave Ember "speaking"
forever (which would block every greeting), a resumed session keeps the same
prompt and the record of memories already given, and a close with any code
(not just the common ones) leads to a reconnect instead of a dead loop.
Prints PASS on success.
"""
import asyncio, contextlib, importlib, os, sys, tempfile, time
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
BAYMAX = os.path.join(HERE, "..", "Baymax-main")
sys.path.insert(0, BAYMAX)
for name in ["mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision",
             "semantic_embedder"]:
    sys.modules[name] = mock.MagicMock(name=name)
sd = mock.MagicMock()
sd.query_devices.return_value = [{"name": "pipewire", "max_output_channels": 0, "max_input_channels": 2},
                                 {"name": "default", "max_output_channels": 2, "max_input_channels": 0}]
sys.modules["sounddevice"] = sd
os.environ.pop("BAYMAX_DEVICE_KEY", None)
import ember_self, person_memory
tmp = tempfile.mkdtemp()
ember_self.PROFILE_PATH = os.path.join(tmp, "p.json"); person_memory.PENDING_PATH = os.path.join(tmp, "c.jsonl")
with mock.patch("subprocess.run"), mock.patch("subprocess.check_output", return_value=b""):
    R = importlib.import_module("realtime_gemini_10")
from google.genai import types

configs, script = [], []          # script: one entry per connect: list of messages, or an Exception
class FakeSession:
    def __init__(self, messages): self.messages = messages
    async def receive(self):
        for m in self.messages:
            await asyncio.sleep(0.05)
            if isinstance(m, Exception):
                raise m
            yield m
        await asyncio.sleep(3600)
    async def send_client_content(self, **kw): pass
    async def send_tool_response(self, **kw): pass
class FakeLive:
    @contextlib.asynccontextmanager
    async def connect(self, model, config):
        configs.append(types.LiveConnectConfig(**config))      # validates like the real SDK
        step = script.pop(0) if script else []
        if isinstance(step, Exception):
            raise step
        yield FakeSession(step)
class FakeClient:
    def __init__(self, **kw):
        self.aio = mock.MagicMock(); self.aio.live = FakeLive()
async def idle(*a, **k):
    await asyncio.sleep(3600)
resets = []
def upd(handle): return types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle=handle, resumable=True))
goaway = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="30s"))
speaking = types.LiveServerMessage(server_content=types.LiveServerContent(model_turn=types.Content(
    parts=[types.Part(inline_data=types.Blob(data=b"\0\0" * 100, mime_type="audio/pcm;rate=24000"))])))

async def main():
    with mock.patch.object(R.genai, "Client", FakeClient), \
         mock.patch.object(R, "start_output_stream", lambda: mock.MagicMock()), \
         mock.patch.object(R.sd, "_terminate", lambda: resets.append("terminate")), \
         mock.patch.object(R.time, "sleep", lambda s: None), \
         mock.patch.object(R, "_notify_webapp", lambda *a, **k: None):
        for name in ("listen_audio", "send_audio_realtime", "send_video_realtime", "fall_alert_monitor",
                     "initiation_monitor", "self_awareness_monitor", "monitor_queues"):
            setattr(R, name, idle)
        # 1. handle, then GoAway → immediate resumed reconnect, no audio reset
        script.extend([[upd("h1"), speaking, goaway], [upd("h2")]])
        task = asyncio.create_task(R.run())
        while not configs:
            await asyncio.sleep(0.01)
        R._memories_given.add("a memory recalled earlier in this conversation")
        t0 = time.monotonic()
        while len(configs) < 2 and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.05)
        assert len(configs) == 2, configs
        first, second = configs
        assert first.context_window_compression.sliding_window is not None
        assert first.session_resumption.handle is None
        assert second.session_resumption.handle == "h1", second.session_resumption
        assert resets == [], "GoAway was treated as a failure (audio reset)"
        assert second.system_instruction == first.system_instruction
        await asyncio.sleep(0.2)
        assert not R._gemini_speaking, "still 'speaking' after the reply was cut off"
        with R._playback_lock: R._playback_buffer = b""      # what was buffered has played out
        assert R._safe_to_initiate(time.monotonic() + 100), "greetings would stay blocked"
        assert "a memory recalled earlier in this conversation" in R._memories_given
        print(f"1 GoAway → resumed with handle h1 after {time.monotonic() - t0:.1f}s, no audio reset")
        await asyncio.sleep(0.3)
        assert R._resume_handle == "h2"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await task

        # 2. the handle keeps failing → dropped after 2 tries → fresh session
        configs.clear()
        script.extend([RuntimeError("1008 invalid handle"), RuntimeError("1008 invalid handle"), []])
        task = asyncio.create_task(R.run())
        t0 = time.monotonic()
        while len(configs) < 3 and time.monotonic() - t0 < 15:
            await asyncio.sleep(0.05)
        assert [c.session_resumption.handle for c in configs] == ["h2", "h2", None], \
            [c.session_resumption.handle for c in configs]
        print("2 failing handle dropped after 2 tries → fresh session")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await task

        # 3. an expired handle isn't used
        configs.clear()
        R._resume_handle, R._resume_handle_t = "old", time.time() - 3 * 3600
        script.extend([[]])
        task = asyncio.create_task(R.run())
        while not configs:
            await asyncio.sleep(0.05)
        assert configs[0].session_resumption.handle is None
        assert "a memory recalled earlier in this conversation" not in R._memories_given
        print("3 expired handle not used; a fresh session starts a fresh record")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await task

        # 4. closed with an unusual code (1007) → reconnect, not an endless retry loop
        configs.clear(); resets.clear()
        R._resume_handle = None
        script.extend([[R.genai_errors.APIError(1007, {"message": "Request contains an invalid argument."})],
                       []])
        task = asyncio.create_task(R.run())
        t0 = time.monotonic()
        while len(configs) < 2 and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.05)
        assert len(configs) == 2, "didn't reconnect after a 1007 close"
        print("4 unusual close code → reconnected")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await task
    print("PASS")

asyncio.run(main())
