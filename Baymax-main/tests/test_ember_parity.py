"""The new core (ember/) against the unchanged legacy core (realtime_gemini_8.py), run side by side.

Both run on the same virtual devices, with the real audio cleanup and waiting
cue, a shared fake clock and the same scripted fake Gemini. Every comparison is
of observable behaviour: what each sends to Gemini, plays on the speaker, puts
on the monitor, posts to the dashboard and records to disk.
"""
import ast
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import runtime_monitor as monitor
from ember.devices import hardware
from ember.devices.virtual import Timing, VirtualCamera, VirtualSoundDevice
from ember.session import config as session_config
from ember.session.engine import Runtime
from ember.settings import Settings
from ember.testing import fake_gemini as fg
from ember.testing import legacy

ROOT = Path(__file__).resolve().parents[1]
MVP_ENV = {"BAYMAX_AUDIO_ENHANCEMENT": "1", "BAYMAX_WAIT_CUE_ENABLED": "0", "BAYMAX_INTERRUPTION_ENABLED": "0",
           "BAYMAX_VOICE_ANALYSIS": "0"}
REAL_SLEEP = time.sleep


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fake_clocks(enhancer, cue, clock):
    if enhancer is not None:
        enhancer.clock = enhancer.residual.clock = clock
    if cue is not None:
        cue.clock = clock


class Pair:
    """A legacy module and a new Runtime, built under the same environment."""

    def __init__(self, env=None, camera=None):
        self.env = {"BAYMAX_HARDWARE_PROFILE": "pc", **(env or {})}
        with patch.dict(os.environ, self.env):
            self.legacy_sd = VirtualSoundDevice(realtime=False)
            self.old = legacy.load(self.legacy_sd, env=self.env)
            self.new_sd = VirtualSoundDevice(realtime=False)
            self.posts = legacy.Posts()
            settings = Settings.load()
            self.new = Runtime(settings, hardware.Devices(self.new_sd, settings), http=self.posts,
                               script_dir=tempfile.mkdtemp(prefix="ember-parity-")).prepare()
        self.clock = Clock()
        fake_clocks(self.old._audio_enhancer, self.old._waiting_cue, self.clock)
        fake_clocks(self.new.enhancer, self.new.waiting_cue, self.clock)


def upstream_values(path, names):
    from google.genai import types
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    namespace = {"types": types}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names \
                and node.targets[0].id not in namespace:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return {name: namespace[name] for name in names}


class ConfigParity(unittest.TestCase):
    def test_model_prompt_and_base_config_match_v8_and_upstream(self):
        names = ["MODEL", "SUMMARY_MODEL", "_BASE_SYSTEM_INSTRUCTION", "_BASE_CONFIG"]
        for path in (ROOT / "realtime_gemini_8.py", ROOT / "upstream/realtime_gemini_8.py"):
            values = upstream_values(path, names)
            self.assertEqual(session_config.MODEL, values["MODEL"])
            self.assertEqual(session_config.SUMMARY_MODEL, values["SUMMARY_MODEL"])
            self.assertEqual(session_config.BASE_SYSTEM_INSTRUCTION, values["_BASE_SYSTEM_INSTRUCTION"])
            self.assertEqual(session_config._RAW_BASE_CONFIG, values["_BASE_CONFIG"], path)

    def test_session_config_is_identical_across_settings(self):
        for env in ({}, MVP_ENV, {**MVP_ENV, "BAYMAX_TOOLKIT_ENABLED": "0"}, {**MVP_ENV, "BAYMAX_GOOGLE_SEARCH": "0"},
                    {"BAYMAX_AUDIO_ENHANCEMENT": "1", "BAYMAX_SPEECH_START_SENSITIVITY": "high"},
                    {**MVP_ENV, "BAYMAX_CONTEXT_COMPRESSION": "0", "BAYMAX_SESSION_RESUMPTION": "0"},
                    {**MVP_ENV, "EMBER_DISCOVERY_ENABLED": "0", "EMBER_EVERYDAY_ENABLED": "0"}):
            with self.subTest(env=env):
                pair = Pair(env)
                with patch.dict(os.environ, pair.env):
                    self.assertEqual(pair.new.session_config(), pair.old._build_config())

    def test_preflight_reads_the_same_models_from_either_core(self):
        from gemini_preflight import configured_models
        self.assertEqual(configured_models(ROOT / "ember/session/config.py"), configured_models(ROOT / "realtime_gemini_8.py"))


class AudioParity(unittest.TestCase):
    def speaker_blocks(self, pair, runtime_new, blocks):
        out_old, out_new = [], []
        for index in range(blocks):
            timing = Timing(currentTime=1.0 + index * 0.02, outputBufferDacTime=1.03 + index * 0.02)
            for callback, sink in ((pair.old._output_callback, out_old), (runtime_new.output_callback, out_new)):
                outdata = np.zeros((1024, 1), dtype=np.int16)
                callback(outdata, 1024, timing, None)
                sink.append(outdata.tobytes())
            pair.clock.now += 1024 / 48000
        return out_old, out_new

    def test_speaker_callback_matches_with_cleanup_and_waiting_cue(self):
        for env in (MVP_ENV, {**MVP_ENV, "BAYMAX_WAIT_CUE_ENABLED": "1"}, {"BAYMAX_AUDIO_ENHANCEMENT": "0"}):
            with self.subTest(env=env):
                pair = Pair(env)
                reply = (np.sin(np.arange(48000) / 9) * 8000).astype(np.int16).tobytes()
                pair.old._append_playback(reply)
                pair.new.append_playback(reply)
                for cue in (pair.old._waiting_cue, pair.new.waiting_cue):
                    if cue is not None:
                        cue.event("thinking")
                out_old, out_new = self.speaker_blocks(pair, pair.new, 60)
                self.assertEqual(out_old, out_new)
                self.assertEqual(pair.old._playback_buffer, pair.new.playback)

    def test_microphone_path_matches_queue_recording_and_cleanup(self):
        for channels, env in ((2, MVP_ENV), (1, MVP_ENV), (2, {"BAYMAX_AUDIO_ENHANCEMENT": "0"})):
            with self.subTest(channels=channels, env=env):
                pair = Pair(env)
                pair.old.INPUT_CHANNELS = channels
                pair.new.devices.input_channels = channels
                sent = {"old": [], "new": []}
                loop = SimpleNamespace(call_soon_threadsafe=None)
                rng = np.random.default_rng(1)
                frames = [(rng.standard_normal((1024, channels)) * 3000).astype(np.int16) for _ in range(120)]

                class Q:
                    def __init__(self, sink):
                        self.sink = sink

                    def put_nowait(self, item):
                        self.sink.append(item["data"])
                pair.old.audio_queue_mic = Q(sent["old"])
                pair.new.audio_queue_mic = Q(sent["new"])
                loop.call_soon_threadsafe = lambda fn, item: fn(item)

                # Legacy: the callback is defined inside listen_audio; run it until the stream opens.
                captured = {}
                pair.legacy_sd.InputStream = lambda **kw: captured.setdefault("cb", kw["callback"]) and _Stop()
                with patch("asyncio.get_running_loop", lambda: loop):
                    with self.assertRaises(_Stopped):
                        asyncio.run(_first_step(pair.old.listen_audio()))
                old_cb = captured["cb"]
                new_cb = pair.new.mic_callback(loop)
                if pair.new.enhancer is not None:
                    pair.new.enhancer.reset()
                for index, frame in enumerate(frames):
                    timing = Timing(currentTime=2.0 + index * 0.02, inputBufferAdcTime=1.99 + index * 0.02)
                    old_cb(frame.copy(), 1024, timing, None)
                    new_cb(frame.copy(), 1024, timing, None)
                    pair.clock.now += 1024 / 48000
                self.assertEqual(sent["old"], sent["new"])
                recorded_old = [pair.old._audio_record_queue.get_nowait() for _ in range(pair.old._audio_record_queue.qsize())]
                recorded_new = [pair.new.recorder.queue.get_nowait() for _ in range(pair.new.recorder.queue.qsize())]
                self.assertEqual(recorded_old, recorded_new)

    def test_send_path_drops_the_same_stale_and_half_duplex_audio(self):
        for env in (MVP_ENV, {"BAYMAX_AUDIO_ENHANCEMENT": "0"}):
            with self.subTest(env=env):
                pair = Pair(env)
                results = {}
                for name, core, send in (("old", pair.old, pair.old.send_audio_realtime),
                                         ("new", pair.new, pair.new.send_audio_realtime)):
                    results[name] = asyncio.run(_drive_send(core, send, name == "old"))
                self.assertEqual(results["old"], results["new"])


class _Stopped(Exception):
    pass


class _Stop:
    latency = 0.0

    def __enter__(self):
        raise _Stopped()

    def __exit__(self, *exc):
        return False


async def _first_step(coroutine):
    await coroutine


async def _drive_send(core, send, is_legacy):
    """Queue fresh, stale and during-playback messages; return what reached Gemini."""
    queue = asyncio.Queue()
    if is_legacy:
        core.audio_queue_mic = queue
    else:
        core.audio_queue_mic = queue
    client = fg.FakeClient()
    async with client.aio.live.connect(model="m", config={}) as session:
        task = asyncio.create_task(send(session))
        now = time.perf_counter()
        items = [(b"\1" * 2048, now), (b"\2" * 2048, now - 30.0), (b"\3" * 2048, now)]
        for data, ts in items:
            queue.put_nowait({"data": data, "mime_type": "audio/pcm;rate=48000", "ts": ts})
        await asyncio.sleep(0.05)
        playback = b"\5" * 4096
        if is_legacy:
            core._append_playback(playback)
        else:
            core.append_playback(playback)
        queue.put_nowait({"data": b"\4" * 2048, "mime_type": "audio/pcm;rate=48000", "ts": time.perf_counter()})
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return client.record["audio_chunks"], client.record["audio_bytes"], sorted(client.record["audio_mime"])


class ReceiveParity(unittest.TestCase):
    SCRIPT = [
        fg.resumption("handle-1"),
        fg.content(heard="Hi Ember"),
        fg.content(heard=" "),
        fg.tool_call("get_weather", {"units": "celsius"}),
        fg.content(said="Hello there.", audio=(np.arange(480, dtype=np.int16) * 7).tobytes(), text="(thinking)"),
        fg.content(audio=np.full(240, 99, dtype=np.int16).tobytes()),
        fg.content(complete=True),
        fg.content(said="Interrupted reply", audio=np.full(240, 5, dtype=np.int16).tobytes()),
        fg.content(interrupted=True),
        fg.message(),
        fg.content(heard="Bye", complete=True),
        fg.drop(1011),
    ]

    def run_core(self, receive, core, is_legacy):
        calls = []
        tools = []
        resumed = []

        async def tool_handler(session, tool_call):
            tools.append([c.name for c in tool_call.function_calls])

        script = fg.Script(fg.Rule(self.SCRIPT, after_connect=True))
        client = fg.FakeClient(script)
        if is_legacy:
            core._last_mic_send_ts = time.perf_counter()
        else:
            core.last_mic_send_ts = time.perf_counter()
        recorder = lambda name: (lambda *args, **kwargs: calls.append((name, args)))
        with patch.object(monitor, "emit", recorder("emit")), patch.object(monitor, "transcribe", recorder("transcribe")), \
                patch.object(monitor, "finish_transcripts", recorder("finish")), \
                patch("features.toolkits.gemini_tools.handle_tool_call", tool_handler), \
                patch("features.conversational_ability.session.on_server_message", lambda r: resumed.append(r)):
            async def go():
                async with client.aio.live.connect(model="m", config={}) as session:
                    await receive(session)
            with self.assertRaises(fg.Closed):
                asyncio.run(go())
        REAL_SLEEP(0.1)  # Let dashboard posts (threads) land.
        return calls, tools, len(resumed)

    def test_receive_loop_matches_events_playback_transcripts_tools_and_posts(self):
        pair = Pair(MVP_ENV)
        old = self.run_core(pair.old.receive_audio, pair.old, True)
        new = self.run_core(pair.new.receive_audio, pair.new, False)
        self.assertEqual(old, new)
        self.assertEqual(pair.old._playback_buffer, pair.new.playback)
        self.assertEqual((pair.old._transcript_user, pair.old._transcript_gemini),
                         (pair.new.transcript_user, pair.new.transcript_gemini))
        self.assertEqual(pair.old._gemini_speaking, pair.new.gemini_speaking)
        self.assertEqual(sorted(map(str, pair.old.http_requests.calls)), sorted(map(str, pair.posts.calls)))
        for name in ("receive", "first_audio", "roundtrip"):
            self.assertEqual(len(getattr(pair.old, "tracker_" + name).samples), len(getattr(pair.new.trackers, name).samples))


class VideoParity(unittest.TestCase):
    def test_frames_sent_to_gemini_are_identical(self):
        pair = Pair(MVP_ENV)
        frame = (np.indices((480, 640)).sum(axis=0)[:, :, None] % 255).astype(np.uint8).repeat(3, axis=2)
        pair.old._latest_frame = frame
        pair.new.video.latest_frame = frame
        sent = []
        for send in (pair.old.send_video_realtime, pair.new.video.send):
            client = fg.FakeClient()

            async def go():
                async with client.aio.live.connect(model="m", config={}) as session:
                    task = asyncio.create_task(send(session))
                    while len(client.record["video"]) < 2:
                        await asyncio.sleep(0)
                    task.cancel()
            real = asyncio.sleep
            with patch("asyncio.sleep", lambda delay, *a: real(0)), legacy.camera(None):
                asyncio.run(go())
            sent.append(client.record["video"][:2])
        self.assertEqual(sent[0], sent[1])

    def test_camera_thread_feeds_the_same_frames_to_fall_detection(self):
        pair = Pair(MVP_ENV)
        frames = [np.full((480, 640, 3), value, dtype=np.uint8) for value in (10, 20, 30)]
        results = []
        for run, set_worker, set_event, latest in (
                (pair.old._fall_detection_thread, lambda w: setattr(pair.old, "fall_worker", w),
                 lambda e: setattr(pair.old, "_shutdown_event", e), lambda: pair.old._latest_frame),
                (pair.new.video.camera_thread, lambda w: setattr(pair.new.video, "fall_worker", w),
                 lambda e: setattr(pair.new.video, "shutdown_event", e), lambda: pair.new.video.latest_frame)):
            worker = FakeFallWorker()
            event = threading.Event()
            set_worker(worker)
            set_event(event)
            capture = VirtualCamera(frames)
            worker.after = lambda count, e=event: count >= 6 and e.set()
            pair.new.devices.camera_opener = None  # The real probe path, as v8: pc_hardware, one test read.
            with legacy.camera(capture):
                run()
            results.append((worker.started, [int(f[0, 0, 0]) for f in worker.frames], worker.stopped,
                            capture.settings, int(latest()[0, 0, 0]), capture.opened))
        self.assertEqual(results[0], results[1])

    def test_no_camera_means_no_capture_on_both(self):
        pair = Pair(MVP_ENV)
        worker = FakeFallWorker()
        pair.old.fall_worker = worker
        with legacy.camera(None):
            pair.old._fall_detection_thread()
        pair.new.devices.camera_opener = lambda: None
        pair.new.video.fall_worker = worker
        with legacy.camera(None):  # Also blocks real cameras for the new core.
            pair.new.video.camera_thread()
        self.assertEqual(worker.started, [])


class FakeFallWorker:
    def __init__(self):
        self.started, self.frames, self.stopped, self.after = [], [], 0, lambda count: None

    def start(self, **settings):
        self.started.append(settings)

    def submit(self, frame):
        self.frames.append(frame)
        self.after(len(self.frames))

    def stop(self):
        self.stopped += 1

    def status(self):
        return {"state": "disabled"}

    async def alert_monitor(self, session, shutdown_event):
        while not shutdown_event.is_set():
            await asyncio.sleep(0.05)


class RecordingParity(unittest.TestCase):
    def test_segments_on_disk_are_identical(self):
        pair = Pair(MVP_ENV)
        chunks = [np.full(4800, i, dtype=np.int16).tobytes() for i in range(30)]
        contents = []
        for setup, run, queue in (
                (lambda d, e: (setattr(pair.old, "DAY_UTTERANCE_DIR", d), setattr(pair.old, "_shutdown_event", e)),
                 pair.old._audio_writer_thread, lambda: pair.old._audio_record_queue),
                (lambda d, e: (setattr(pair.new.recorder, "directory", d), setattr(pair.new.recorder, "shutdown_event", e)),
                 pair.new.recorder.run, lambda: pair.new.recorder.queue)):
            with tempfile.TemporaryDirectory() as directory:
                stale = Path(directory, "seg_20000101_000000_0000.wav")
                stale.write_bytes(b"old")
                os.utime(stale, (0, 0))
                setup(directory, threading.Event())
                for chunk in chunks:
                    queue().put(chunk)
                queue().put(None)
                with patch("time.strftime", lambda fmt, *a: "20261104_120000"):
                    run()
                contents.append(sorted((p.name, p.read_bytes()) for p in Path(directory).iterdir()))
        self.assertEqual(contents[0], contents[1])
        self.assertFalse(any(name.startswith("seg_2000") for name, _ in contents[0]))  # Pruned on startup.


class SessionParity(unittest.TestCase):
    """Whole sessions on virtual devices: connect, talk, a 1011 drop, reconnect."""

    def run_session(self, start, client):
        events = []

        def sleep(seconds):
            REAL_SLEEP(min(seconds, 0.02))
        with patch.object(monitor, "start", lambda sample=None: None), \
                patch("features.toolkits.gemini_tools.prewarm", lambda: None), \
                patch.object(monitor, "emit", lambda state, message: events.append((state, message))), \
                patch("time.sleep", sleep), legacy.camera(None):
            with self.assertRaises(fg.StopRuntime):
                asyncio.run(start())
        return events

    def scripts(self):
        reply = fg.content(said="Hi!", audio=np.full(4800, 300, dtype=np.int16).tobytes())
        first = fg.Script(fg.Rule([fg.content(heard="Hello"), reply, fg.content(complete=True)], audio=48000),
                          fg.Rule([fg.drop(1011)], audio=1))
        second = fg.Script(fg.Rule([fg.content(heard="Again"), reply, fg.content(complete=True)], audio=48000),
                           fg.Rule([fg.drop(1006)], audio=1))
        return first, second

    def test_connect_converse_drop_and_reconnect_match(self):
        outcomes = []
        for core in ("old", "new"):
            with patch.dict(os.environ, {"BAYMAX_HARDWARE_PROFILE": "pc", **MVP_ENV}):
                sd = VirtualSoundDevice(realtime=True, speed=4.0)
                client = fg.FakeClient(*self.scripts(), max_connects=2)
                worker = FakeFallWorker()
                if core == "old":
                    module = legacy.load(sd, env=MVP_ENV)
                    legacy.use_client(module, client)
                    module.fall_worker = worker
                    start = module.run
                    posts = module.http_requests
                else:
                    settings = Settings.load()
                    posts = legacy.Posts()
                    runtime = Runtime(settings, hardware.Devices(sd, settings), client_factory=lambda: client,
                                      http=posts, fall_worker=worker, script_dir=tempfile.mkdtemp()).prepare()
                    start = runtime.run
                events = self.run_session(start, client)
            REAL_SLEEP(0.1)
            states = [state for state, _ in events]
            outcomes.append({
                "connects": len(client.connects),
                "configs_equal": all(c["config"] == client.connects[0]["config"] for c in client.connects),
                "models": {c["model"] for c in client.connects},
                "states": [s for i, s in enumerate(states) if i == 0 or states[i - 1] != s][:8],
                "mime": client.record["audio_mime"],
                "heard_audio": client.record["audio_bytes"] > 96000,
                "spoke": sd.speaker.voiced_blocks > 0,
                "resets": sd.resets,
                "boot": [json["stage"] for url, json in posts.calls if url.endswith("/api/boot-status")][:3],
            })
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0]["connects"], 3)  # Two sessions, then the stop.
        self.assertTrue(outcomes[0]["spoke"])
        self.assertEqual(outcomes[0]["resets"], 2)


if __name__ == "__main__":
    unittest.main()
