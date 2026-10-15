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
            seen.append(m)
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
played_after_goaway = [0]
_real_append = R._append_playback
def _count_playback(data):
    if any(m.go_away for m in seen): played_after_goaway[0] += 1
    _real_append(data)
seen = []
def upd(handle): return types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle=handle, resumable=True))
goaway = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="30s"))
short = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="4s"))
speaking = types.LiveServerMessage(server_content=types.LiveServerContent(model_turn=types.Content(
    parts=[types.Part(inline_data=types.Blob(data=b"\0\0" * 100, mime_type="audio/pcm;rate=24000"))])))

async def main():
    with mock.patch.object(R.genai, "Client", FakeClient), \
         mock.patch.object(R, "start_output_stream", lambda: mock.MagicMock()), \
         mock.patch.object(R.sd, "_terminate", lambda: resets.append("terminate")), \
         mock.patch.object(R.time, "sleep", lambda s: None), \
         mock.patch.object(R, "_notify_webapp", lambda *a, **k: None), \
         mock.patch.object(R, "_append_playback", _count_playback):
        real_self_awareness = R.self_awareness_monitor
        for name in ("listen_audio", "send_audio_realtime", "send_video_realtime", "fall_alert_monitor",
                     "initiation_monitor", "self_awareness_monitor", "monitor_queues"):
            setattr(R, name, idle)
        # 1. handle, then GoAway → immediate resumed reconnect, no audio reset
        # (a reply still going when the GoAway time runs out is cut off)
        script.extend([[upd("h1"), speaking, short], [upd("h2")]])
        task = asyncio.create_task(R.run())
        while not configs:
            await asyncio.sleep(0.01)
        R._memories_given["a memory recalled earlier in this conversation"] = time.monotonic()
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

        # 5. GoAway while Ember is talking: it finishes the reply first, then moves
        configs.clear()
        R._resume_handle = None
        done = types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))
        # Handles sent while Ember talks can't be resumed from; it waits for the
        # one after the reply (g2), so the new connection includes that reply.
        script.extend([[upd("g1"), speaking, goaway, speaking, speaking, done, upd("g2")], []])
        task = asyncio.create_task(R.run())
        t0 = time.monotonic()
        while len(configs) < 2 and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.01)
        assert len(configs) == 2 and configs[1].session_resumption.handle == "g2", \
            [c.session_resumption.handle for c in configs]
        assert played_after_goaway[0] >= 2, "the reply was cut off at the GoAway"
        print("5 GoAway mid-reply → finished the reply, then resumed")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await task

        # 6. …but not past the server's deadline if the reply never ends
        configs.clear()
        R._resume_handle = None
        script.extend([[upd("g2"), speaking, short], []])
        task = asyncio.create_task(R.run())
        t0 = time.monotonic()
        while len(configs) < 2 and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.01)
        waited = time.monotonic() - t0
        assert len(configs) == 2 and 0.9 < waited < 4, waited
        print(f"6 reply that never ends → moved on after {waited:.1f}s, before the server's cut-off")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await task

        # 6b. GoAway just after a reply ended, before its handle: wait for that handle
        #     (6c) and when nothing new happened since the last handle: move at once
        for label, steps, want in (
                ("6b", [upd("k1"), speaking, done, goaway, upd("k2")], "k2"),
                ("6c", [upd("k3"), speaking, done, upd("k4"), goaway], "k4")):
            configs.clear()
            R._resume_handle = None
            script.extend([steps, []])
            task = asyncio.create_task(R.run())
            t0 = time.monotonic()
            while len(configs) < 2 and time.monotonic() - t0 < 10:
                await asyncio.sleep(0.01)
            assert len(configs) == 2 and configs[1].session_resumption.handle == want, \
                (label, [c.session_resumption.handle for c in configs])
            print(f"{label} GoAway after a finished reply → resumed with {want} "
                  f"after {time.monotonic() - t0:.1f}s")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await task
        # 7. The family edits the profile while Ember is reconnecting: the
        #    resumed session keeps its prompt, so the edit is passed on in it
        ember_self.save_profile({"user_name": "Margaret"})
        R._resume_handle, R._session_config = None, None
        R._build_config()                                    # fresh session, built from "Margaret"
        time.sleep(0.01)
        ember_self.save_profile({"user_name": "Maggie"})     # e.g. synced from the cloud on reconnect
        R._resume_handle = "r1"
        resumed = R._build_config()
        assert "Margaret" in resumed["system_instruction"] and R._profile["user_name"] == "Margaret"
        class Recorder:
            def __init__(self): self.sent = []
            async def send_client_content(self, *, turns, turn_complete):
                self.sent.append(turns["parts"][0]["text"])
        rec = Recorder()
        with R._initiation_ts_lock: R._last_loud_mic_ts = 0.0
        with R._playback_lock: R._playback_buffer = b""      # earlier stages' audio has played
        with R._gemini_speaking_lock: R._gemini_speaking = False
        R._last_opener_ts = 0.0
        mon = asyncio.create_task(real_self_awareness(rec))
        t0 = time.monotonic()
        while R._profile["user_name"] != "Maggie" and time.monotonic() - t0 < 5:
            await asyncio.sleep(0.05)
        mon.cancel()
        assert R._profile["user_name"] == "Maggie", R._profile
        assert any("Maggie" in m for m in rec.sent), rec.sent
        print("7 profile edited during a reconnect → passed on in the resumed session")

        # 8. A failure while preparing a conversation opener doesn't end the session
        class Boom:
            def people_here(self): raise OSError("disk full")
        with mock.patch.object(R, "_face_id", Boom()):
            assert await R._fire_initiation(rec) is False
        print("8 opener failure contained")

        # 9. After a forget the next connection starts fresh — even if the live
        #    session hands out another resumption handle meanwhile
        R._build_config()
        R._start_fresh_next_time()
        R._resume_handle = "issued-after-the-forget"
        fresh = R._build_config()
        assert fresh["session_resumption"]["handle"] is None and R._resume_handle is None
        print("9 forget → next connection is a fresh conversation")

        # 10. If the server doesn't accept background (non-blocking) tools,
        #     Ember reconnects at once with standard ones instead of failing
        class Faces:
            profile = {}
            store = mock.MagicMock()
            def describe_in_view(self): return "Nobody is in front of your camera right now."
            def describe_visits(self): return None
        configs.clear(); resets.clear()
        R._resume_handle, R._session_config = None, None
        R._tools_non_blocking, R._non_blocking_confirmed = True, False
        script.extend([RuntimeError("received 1007 (invalid frame payload data) Request contains an invalid argument."), []])
        with mock.patch.object(R, "_face_id", Faces()):
            task = asyncio.create_task(R.run())
            t0 = time.monotonic()
            while len(configs) < 2 and time.monotonic() - t0 < 10:
                await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await task
        behaviors = [[d.behavior for d in c.tools[0].function_declarations] for c in configs]
        assert len(configs) == 2 and set(behaviors[0]) == {types.Behavior.NON_BLOCKING} \
            and set(behaviors[1]) == {None}, behaviors
        assert resets == [] and time.monotonic() - t0 < 4, "fell back slowly (audio reset)"
        assert not R._tools_non_blocking
        print("10 background tools rejected → reconnected at once with standard tools")
    print("PASS")

asyncio.run(main())
