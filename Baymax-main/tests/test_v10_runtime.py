"""The MVP launcher runs realtime_gemini_10.py: its real hooks, without devices or Gemini."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from google import genai
from google.genai import errors, live
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from features.conversational_ability.addressing import AddressGate
from features.conversational_ability.session import connection_lost
import test_waiting_cue
from test_public_core_integration import load_function

ROOT = Path(__file__).resolve().parents[1]
V10 = ROOT / "realtime_gemini_10.py"


class V10WaitingCueTests(test_waiting_cue.WaitingCueTests):
    """Every waiting-cue test, against v10's speaker callback and microphone hook."""
    def setUp(self):
        super().setUp()
        patcher = patch.object(test_waiting_cue, "CORE", V10)
        patcher.start()
        self.addCleanup(patcher.stop)


class V10RuntimeTests(unittest.TestCase):
    def source(self):
        return V10.read_text(encoding="utf-8")

    def test_launcher_runs_v10(self):
        import run_pc
        self.assertEqual(run_pc.RUNTIME, V10)

    def test_builtin_fall_checkin_reengages_the_microphone(self):
        from features.conversational_ability.engage import engagement
        class FallEvent:
            count = 0
            def wait(self, timeout):
                self.count += 1
                if self.count > 1:
                    raise asyncio.CancelledError()
                return True
            def clear(self):
                pass
        with patch('test_public_core_integration.CORE', V10):
            alert = load_function('fall_alert_monitor', {'asyncio': asyncio, '_fall_detected_event': FallEvent()})
        session = AsyncMock()
        with patch.multiple(engagement, disengaged=True, listener=None):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(alert(session))
            self.assertFalse(engagement.disengaged)
        session.send_client_content.assert_awaited_once()

    def test_queued_replies_have_a_gap_but_chunks_and_idle_replies_do_not(self):
        namespace = {"_playback_buffer": b"", "_playback_lock": threading.Lock(),
                     "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1}
        append = load_function("_append_playback", namespace)
        flush = load_function("_flush_playback", namespace)
        first, second = b"\x01\x00" * 4, b"\x02\x00" * 4
        append(first, new_turn=True)
        self.assertEqual(namespace['_playback_buffer'], first)
        append(first)
        self.assertEqual(namespace['_playback_buffer'], first * 2)
        append(second, new_turn=True)
        self.assertEqual(namespace['_playback_buffer'], first * 2 + b"\x00" * 17280 + second)
        flush()
        append(second, new_turn=True)
        self.assertEqual(namespace['_playback_buffer'], second)

    def test_speaker_mixes_extra_sources_such_as_audiobooks(self):
        enhancer = SimpleNamespace(full_duplex=True, rendered=Mock(), mix_output=lambda chunk: b"\x07\x00" * (len(chunk) // 2))
        namespace = {"_playback_buffer": b"", "_playback_lock": threading.Lock(), "_waiting_cue": None,
                     "_audio_enhancer": enhancer, "np": __import__("numpy"), "OUTPUT_CHANNELS": 1}
        with patch("test_public_core_integration.CORE", V10):
            callback = load_function("_output_callback", namespace)
        out = __import__("numpy").empty((4, 1), dtype="int16")
        callback(out, 4, None, None)
        self.assertEqual(out.tobytes(), b"\x07\x00" * 4)
        enhancer.rendered.assert_called_once_with(b"\x07\x00" * 4, None)

    def test_stale_microphone_audio_is_dropped(self):
        async def exercise(stale):
            mic_queue = asyncio.Queue()
            await mic_queue.put({"data": b"pcm-test", "mime_type": "audio/pcm;rate=48000"})
            sent = []
            async def send(**kwargs):
                sent.append(kwargs["audio"].data)
                raise asyncio.CancelledError()
            with patch("test_public_core_integration.CORE", V10):
                fn = load_function("send_audio_realtime", {"asyncio": asyncio, "time": time,
                    "MIC_SEND_TIMEOUT_S": 5, "audio_queue_mic": mic_queue, "tracker_mic_queue": SimpleNamespace(record=Mock()),
                    "tracker_send_audio": SimpleNamespace(record=Mock()), "_playback_lock": threading.Lock(),
                    "_playback_buffer": b"", "_audio_enhancer": SimpleNamespace(full_duplex=True, stale=lambda ms: stale),
                    "types": SimpleNamespace(Blob=lambda **kw: SimpleNamespace(**kw))})
            try:
                await asyncio.wait_for(fn(SimpleNamespace(send_realtime_input=send)), .02)
            except (asyncio.CancelledError, TimeoutError):
                pass
            return sent
        self.assertEqual(asyncio.run(exercise(False)), [b"pcm-test"])
        self.assertEqual(asyncio.run(exercise(True)), [])

    def test_receive_loop_passes_resumption_and_goaway_to_session(self):
        update = SimpleNamespace(resumable=True, new_handle="h")
        class Session:
            count = 0
            def receive(self):
                async def stream():
                    self.count += 1
                    if self.count > 1:
                        raise asyncio.CancelledError()
                    yield SimpleNamespace(tool_call=None, server_content=None,
                                          session_resumption_update=update, go_away=None)
                return stream()
        with patch("test_public_core_integration.CORE", V10), \
                patch("features.conversational_ability.session.on_server_message") as seen:
            receive = load_function("receive_audio", {"asyncio": asyncio, "time": time,
                "_TurnTranscript": lambda *a: SimpleNamespace(flush=lambda: None)})
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(receive(Session()))
        self.assertEqual(seen.call_args.args[0].session_resumption_update, update)

    def test_mvp_persona_toolkit_and_session_hooks_are_wired(self):
        source = self.source()
        for hook in ("_BASE_SYSTEM_INSTRUCTION = persona.PERSONA",
                     "from features.toolkits import gemini_tools as public_toolkit",
                     "public_toolkit.prewarm()",
                     "fall_worker.submit(frame)",
                     "fall_worker.alert_monitor(live_session, _shutdown_event)",
                     "monitor.notify('fall_alert')",
                     "monitor.start(",
                     'if os.getenv("BAYMAX_VOICE_ANALYSIS", "1") != "0":\n                _run_voice_analysis()'):
            self.assertIn(hook, source)

    def test_only_when_addressed_adds_proactive_audio_and_instruction(self):
        source = self.source()
        self.assertIn('**({"proactivity": {"proactive_audio": True}} if ONLY_WHEN_ADDRESSED else {})', source)
        self.assertIn("[WHEN TO SPEAK]", source)
        for hook in ("live_session = _address_gate.watch(live_session)", "_address_gate.tool_call()",
                     "_address_gate.heard(text)", "_address_gate.reply(show_reply_text, text)",
                     "_address_gate.reply(play_reply_audio)", "_address_gate.end()",
                     "_address_gate.end(interrupted=True)"):
            self.assertIn(hook, source)
        import device_config
        self.assertIs(device_config.DEFAULTS["conversation"]["only_when_addressed"], True)

    def test_random_idle_initiation_is_opt_in(self):
        source = self.source()
        self.assertIn('RANDOM_INITIATION_ENABLED = os.getenv("BAYMAX_RANDOM_INITIATION", "0") == "1"', source)
        self.assertIn("if RANDOM_INITIATION_ENABLED:\n                            tg.create_task(initiation_monitor(live_session))", source)


class ReconnectTests(unittest.TestCase):
    """Any disconnect must end the session so run() reconnects: Ember must never sit
    on a dead connection, and audio from the old reply must not outlive it."""

    def sender(self, queue, emit=None, timeout=5):
        with patch("test_public_core_integration.CORE", V10):
            return load_function("send_audio_realtime", {"asyncio": asyncio, "time": time,
                "audio_queue_mic": queue, "tracker_mic_queue": SimpleNamespace(record=Mock()),
                "tracker_send_audio": SimpleNamespace(record=Mock()), "_playback_lock": threading.Lock(),
                "_playback_buffer": b"", "_audio_enhancer": None, "_last_mic_send_ts": 0.0,
                "monitor": SimpleNamespace(emit=emit or Mock()), "MIC_SEND_TIMEOUT_S": timeout,
                "types": SimpleNamespace(Blob=lambda **kw: SimpleNamespace(**kw))})

    def receiver(self, transcripts=None):
        with patch("test_public_core_integration.CORE", V10):
            return load_function("receive_audio", {"asyncio": asyncio, "time": time,
                "RECEIVE_FAILURES_BEFORE_RECONNECT": 3, "connection_lost": connection_lost,
                "monitor": SimpleNamespace(emit=Mock()),
                "_TurnTranscript": lambda speaker: (transcripts or {}).get(speaker, SimpleNamespace(flush=lambda: None))})

    @staticmethod
    def mic(*blocks):
        queue = asyncio.Queue()
        for block in blocks:
            queue.put_nowait({"data": block, "mime_type": "audio/pcm;rate=48000", "ts": time.perf_counter()})
        return queue

    def test_any_send_failure_ends_the_session(self):
        async def exercise():
            emit, sent = Mock(), []
            async def send(**kwargs):
                sent.append(kwargs)
                raise RuntimeError("anything at all")
            with self.assertRaisesRegex(RuntimeError, "anything at all"):
                await asyncio.wait_for(self.sender(self.mic(b"a", b"b"), emit)(SimpleNamespace(send_realtime_input=send)), 1)
            self.assertEqual(len(sent), 1)  # gives up at once, not one error per mic block
            self.assertEqual(emit.call_args.args[0], "reconnecting")
        asyncio.run(exercise())

    def test_receive_keeps_the_session_after_an_error_handling_one_message(self):
        class Session:
            count = 0
            def receive(self):
                async def stream():
                    self.count += 1
                    if self.count > 4:
                        raise asyncio.CancelledError()
                    yield SimpleNamespace(tool_call=None, server_content=None, go_away=None,
                                          session_resumption_update=None)
                    raise ValueError("bug handling one message")
                return stream()
        session = Session()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.receiver()(session))
        self.assertEqual(session.count, 5)  # messages keep arriving, so no reconnect

    def test_receive_gives_up_when_nothing_arrives_after_unknown_errors(self):
        class Session:
            count = 0
            def receive(self):
                async def stream():
                    self.count += 1
                    if self.count > 10:
                        raise asyncio.CancelledError()  # kept retrying a dead connection: fails below
                    raise ValueError("unknown failure")
                    yield
                return stream()
        session = Session()
        with patch("asyncio.sleep", AsyncMock()), self.assertRaisesRegex(ValueError, "unknown failure"):
            asyncio.run(self.receiver()(session))
        self.assertEqual(session.count, 3)

    def test_cancelled_receive_still_posts_the_partial_transcript(self):
        said = {"user": SimpleNamespace(flush=Mock()), "gemini": SimpleNamespace(flush=Mock())}
        class Session:
            def receive(self):
                async def stream():
                    raise asyncio.CancelledError()
                    yield
                return stream()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.receiver(said)(Session()))
        said["user"].flush.assert_called_once()
        said["gemini"].flush.assert_called_once()

    def test_a_real_closed_websocket_ends_both_loops_for_every_close_code(self):
        """The real SDK session over a real local websocket, closed by the server."""
        async def session_over(code):
            async def close_it(connection):
                if code == 1006:
                    connection.transport.abort()  # no close frame: a dropped network
                else:
                    await connection.close(code, "bye")  # no word the old text filter matched
                await connection.wait_closed()
            async with ws_serve(close_it, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with ws_connect(f"ws://127.0.0.1:{port}", proxy=None) as socket:
                    await socket.wait_closed()
                    client = genai.Client(api_key="test", http_options={"api_version": "v1alpha"})
                    session = live.AsyncSession(api_client=client._api_client, websocket=socket)
                    tasks = {"send": self.sender(self.mic(b"\0\0" * 32)), "receive": self.receiver()}
                    for name, loop in tasks.items():
                        with self.subTest(code=code, loop=name), patch("builtins.print"):
                            # A timeout here means the loop kept retrying the dead connection.
                            with self.assertRaises((errors.APIError, ConnectionClosed)):
                                await asyncio.wait_for(loop(session), 2)
        for code in (1000, 1001, 1006, 1007, 1008, 1011, 1012, 1013):
            asyncio.run(session_over(code))

    def test_a_network_gone_silent_ends_the_session_and_the_socket_can_be_dropped(self):
        """No close, no reset: the peer just stops reading. A send then waits forever,
        and so would the polite close when leaving connect()."""
        async def exercise():
            release = asyncio.Event()
            async def never_read(connection):
                await release.wait()
                connection.transport.abort()
            async with ws_serve(never_read, "127.0.0.1", 0, max_queue=1, max_size=None, write_limit=1024) as server:
                port = server.sockets[0].getsockname()[1]
                socket = await ws_connect(f"ws://127.0.0.1:{port}", proxy=None, write_limit=1024,
                                          ping_interval=None, compression=None)
                client = genai.Client(api_key="test", http_options={"api_version": "v1alpha"})
                session = live.AsyncSession(api_client=client._api_client, websocket=socket)
                blocks = [os.urandom(1 << 20) for _ in range(40)]  # far beyond any socket buffer
                try:
                    with patch("builtins.print"), self.assertRaises(TimeoutError):
                        await asyncio.wait_for(self.sender(self.mic(*blocks), timeout=.2)(session), 10)
                    namespace = {}
                    with patch("test_public_core_integration.CORE", V10):
                        load_function("_abort_connection", namespace)(session)
                    await asyncio.wait_for(socket.wait_closed(), 2)
                finally:
                    release.set()
        asyncio.run(exercise())

    def abandon_namespace(self, gate, audio=b"old reply", speaking=True):
        namespace = {"_playback_lock": threading.Lock(), "_playback_buffer": audio, "_cut_off_prompts": [],
                     "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": speaking, "_address_gate": gate}
        with patch("test_public_core_integration.CORE", V10):
            load_function("_flush_playback", namespace)
            load_function("_abandon_reply", namespace)
        return namespace

    def test_abandoning_a_reply_clears_its_audio_and_keeps_what_must_be_said_again(self):
        gate = SimpleNamespace(abandon=Mock(return_value=[("message", {})]))
        namespace = self.abandon_namespace(gate)
        namespace["_abandon_reply"]()
        self.assertEqual(namespace["_playback_buffer"], b"")
        self.assertFalse(namespace["_gemini_speaking"])
        gate.abandon.assert_called_once_with(True)
        self.assertEqual(namespace["_cut_off_prompts"], [("message", {})])

    def test_nothing_was_cut_off_when_the_reply_had_finished_playing(self):
        gate = SimpleNamespace(abandon=Mock(return_value=[]))
        self.abandon_namespace(gate, audio=b"", speaking=False)["_abandon_reply"]()
        gate.abandon.assert_called_once_with(False)

    def test_a_fall_check_in_cut_off_by_a_disconnect_is_asked_again_through_run(self):
        """The real run(), with its real cleanup, the real gate and the real watched session."""
        class Stop(BaseException):
            pass
        alert = {"turns": {"role": "user", "parts": [{"text": "[FALL ALERT] check on them"}]}, "turn_complete": True}
        sessions, sent = [], []

        class Session:
            def __init__(self):
                self.sent = []
                self._ws = SimpleNamespace(transport=SimpleNamespace(abort=Mock()))
            async def send_client_content(self, **kwargs):
                self.sent.append(kwargs)

        class Connect:
            async def __aenter__(self):
                if len(sessions) == 2:
                    raise Stop()
                sessions.append(Session())
                return sessions[-1]
            async def __aexit__(self, *exc):
                return False

        async def forever(*args, **kwargs):
            await asyncio.Event().wait()

        async def receive(session):
            if len(sessions) == 1:  # Gemini streams the check-in; it is still queued when the line drops.
                await session.send_client_content(**alert)
                namespace["_playback_buffer"] = b"are you okay?"
            raise errors.APIError(1008, {"message": "bye"})

        namespace = {
            "asyncio": asyncio, "time": SimpleNamespace(sleep=Mock(), monotonic=time.monotonic),
            "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_cut_off_prompts": [],
            "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
            "_initiation_ts_lock": threading.Lock(), "_last_robot_speech_ts": 0.0,
            "_address_gate": AddressGate(enabled=False), "monitor": Mock(), "public_toolkit": Mock(),
            "genai": SimpleNamespace(Client=lambda **kw: SimpleNamespace(aio=SimpleNamespace(
                live=SimpleNamespace(connect=lambda **kw: Connect())))),
            "API_KEY": "test", "MODEL": "m", "_build_config": dict, "_boot_status": Mock(),
            "_memory_embedder": None, "MEMORY_RETRIEVAL_ENABLED": False, "FALL_DETECTION_ENABLED": False,
            "FALL_ANGLE_THRESHOLD": 0, "FALL_STAY_DOWN_SECONDS": 0, "FALL_DETECTION_FPS": 0,
            "POSE_TRACKING_ENABLED": True, "RANDOM_INITIATION_ENABLED": False,
            "start_output_stream": Mock(), "sd": Mock(), "get_default_device_id": lambda: (0, 0),
            "listen_audio": forever, "send_audio_realtime": forever, "send_video_realtime": forever,
            "receive_audio": receive, "fall_alert_monitor": forever, "monitor_queues": forever,
            "fall_worker": SimpleNamespace(alert_monitor=forever), "_shutdown_event": None,
        }
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_flush_playback", "_abandon_reply", "_send_cut_off_prompts", "_abort_connection", "run"):
                load_function(name, namespace)
        with patch("builtins.print"), patch("asyncio.sleep", AsyncMock()), self.assertRaises(Stop):
            asyncio.run(namespace["run"]())
        self.assertEqual(sessions[0].sent, [alert])
        self.assertEqual(sessions[1].sent, [alert])  # asked again on the new connection
        self.assertEqual(namespace["_playback_buffer"], b"")  # the old audio did not survive
        sessions[0]._ws.transport.abort.assert_called_once()

    def test_a_fall_alert_that_cannot_reach_gemini_is_asked_again(self):
        event = threading.Event()
        event.set()
        attempts = []

        async def unreachable(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 2:
                raise asyncio.CancelledError()  # the session ends
            raise ConnectionResetError()
        namespace = {"asyncio": asyncio, "_fall_detected_event": event, "print": lambda *a, **k: None}
        with patch("test_public_core_integration.CORE", V10):
            monitor_fn = load_function("fall_alert_monitor", namespace)
        with patch("features.conversational_ability.engage.engagement"), patch("asyncio.sleep", AsyncMock()), \
                self.assertRaises(asyncio.CancelledError):
            asyncio.run(monitor_fn(SimpleNamespace(send_client_content=unreachable)))
        self.assertEqual(len(attempts), 2)
        self.assertTrue(event.is_set())  # still pending for the next connection

    def test_a_failed_resend_stays_queued_for_the_next_connection(self):
        namespace = {"_cut_off_prompts": [((), {"turn_complete": True}), ((), {"turn_complete": True, "n": 2})]}
        with patch("test_public_core_integration.CORE", V10):
            send = load_function("_send_cut_off_prompts", namespace)
        async def fails(**kwargs):
            raise ConnectionResetError()
        with patch("builtins.print"), self.assertRaises(ConnectionResetError):
            asyncio.run(send(SimpleNamespace(send_client_content=fails)))
        self.assertEqual(namespace["_cut_off_prompts"], [((), {"turn_complete": True, "n": 2})])

    def test_a_stop_reaches_the_shutdown_summary_after_a_reconnect(self):
        # After a TaskGroup child fails, Python 3.11/3.12 end asyncio.run with
        # CancelledError on a stop, not KeyboardInterrupt; the summary must still run.
        source = V10.read_text(encoding="utf-8")
        main = source[source.index('if __name__ == "__main__":'):]
        self.assertIn("except (KeyboardInterrupt, asyncio.CancelledError):", main)
        self.assertLess(main.index("except (KeyboardInterrupt"), main.index("_summarise_and_embed()"))

    def test_every_session_end_abandons_the_reply_and_drops_the_socket_before_leaving_connect(self):
        source = V10.read_text(encoding="utf-8")
        run = source[source.index("async def run():"):source.index("def _boot_status")]
        cleanup = run[run.index("finally:"):]
        self.assertLess(cleanup.index("_abandon_reply()"), cleanup.index("_abort_connection(live_session)"))
        self.assertLess(cleanup.index("_abort_connection(live_session)"), cleanup.index("output_stream.stop()"))


class TranscriptsStayOnRobotTests(unittest.TestCase):
    def test_cloud_never_queues_transcripts_and_drops_old_ones(self):
        import baymax_cloud
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(baymax_cloud, "DEVICE_KEY", "999.test"), \
                patch.object(baymax_cloud, "QUEUE_DIR", temp), \
                patch.object(baymax_cloud, "start", lambda: None):
            baymax_cloud.notify("/api/transcript", {"speaker": "user", "text": "private"})
            baymax_cloud.notify("/api/fall", {"trunk_angle": 80})
            baymax_cloud.notify("/api/voice-analysis", {"session": {}, "summary": {}})
            import json
            queued = [json.loads(p.read_text())["endpoint"] for p in sorted(Path(temp).glob("*.json"))]
        self.assertEqual(queued, ["/api/device/fall", "/api/device/voice-analysis"])
        self.assertTrue(baymax_cloud._send({"endpoint": "/api/device/transcript", "data": {}, "queued_at": 0}))

    def test_v10_sends_transcripts_to_the_local_dashboard_in_cloud_mode(self):
        cloud = SimpleNamespace(notify=Mock(), ROBOT_ONLY={"/api/transcript"})
        posted = []
        with patch("test_public_core_integration.CORE", V10):
            notify = load_function("_notify_webapp", {
                "_CLOUD_ENABLED": True, "baymax_cloud": cloud, "WEBAPP_URL": "http://localhost:5000",
                "http_requests": SimpleNamespace(post=lambda url, **kw: posted.append(url)),
                "threading": SimpleNamespace(Thread=lambda target, daemon: SimpleNamespace(start=target))})
        notify("/api/transcript", {"speaker": "user", "text": "private"})
        notify("/api/fall", {})
        self.assertEqual(posted, ["http://localhost:5000/api/transcript"])
        cloud.notify.assert_called_once_with("/api/fall", {})


if __name__ == "__main__":
    unittest.main()
