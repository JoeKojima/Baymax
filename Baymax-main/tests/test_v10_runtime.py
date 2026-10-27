"""The MVP launcher runs realtime_gemini_10.py: its real hooks, without devices or Gemini."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
                    "audio_queue_mic": mic_queue, "tracker_mic_queue": SimpleNamespace(record=Mock()),
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


class LiveViewTests(unittest.TestCase):
    """The dashboard's annotated picture costs 7-20 ms a frame: drawn only for a viewer."""

    def test_camera_loop_draws_only_while_watched_and_shares_pose(self):
        source = V10.read_text(encoding="utf-8")
        start = source.index("def _fall_detection_thread")
        loop = source[start:source.index("\n# ───", start)]
        self.assertIn("watched = _mjpeg_viewers > 0", loop)
        self.assertIn("viz = _draw_annotated(frame, landmarks, result, fps_history, angle_history)", loop)
        self.assertNotIn("_draw_skeleton(", loop)               # all drawing lives in _draw_annotated
        self.assertIn("fall_worker.share(landmarks", loop)      # the fall toolkit reuses this pose
        self.assertIn("shared_pose=POSE_TRACKING_ENABLED", loop)

    def test_stream_counts_viewers_reuses_jpegs_and_draws_a_skipped_frame(self):
        import socket
        import urllib.request
        import cv2
        import numpy as np
        drawn = []
        def draw(frame, *rest):
            drawn.append(rest)
            return np.full((48, 64, 3), 90, np.uint8)
        ns = {"cv2": cv2, "time": time, "_shutdown_event": threading.Event(), "_annotated_frame_lock": threading.Lock(),
              "_annotated_frame": None, "_annotated_inputs": None, "_mjpeg_viewers": 0, "_draw_annotated": draw}
        start = load_function("_start_mjpeg_server", ns)
        self.addCleanup(ns["_shutdown_event"].set)
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        with patch("builtins.print"):
            threading.Thread(target=start, args=(port,), daemon=True).start()
            time.sleep(0.3)

        def parts(count, between=None):
            stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=5)
            seen = []
            for i in range(count):
                if between and i in between:
                    between[i]()
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    byte = stream.read(1)
                    self.assertTrue(byte, "stream closed")
                    header += byte
                seen.append(stream.read(int(header.split(b"Content-Length: ")[1].split(b"\r\n")[0])))
                stream.read(2)
            stream.close()
            return seen

        def show(value):
            with ns["_annotated_frame_lock"]:
                ns["_annotated_frame"] = np.full((48, 64, 3), value, np.uint8)
        # Nobody was watching, so the latest frame wasn't drawn: the viewer draws it once.
        ns["_annotated_inputs"] = ("frame", None, {"fall_active": False}, (15.0,), ())
        first = parts(2)
        self.assertEqual(len(drawn), 1)
        self.assertEqual(first[0], first[1])                     # re-sent, not re-drawn or re-encoded
        time.sleep(0.3)
        self.assertEqual(ns["_mjpeg_viewers"], 0)               # the viewer left
        # A new picture each frame while watched: the stream follows it.
        show(30)
        seen = parts(8, {3: lambda: show(200)})
        means = [int(cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR).mean()) for b in seen]
        self.assertLessEqual(abs(means[0] - 30), 2)
        self.assertLessEqual(abs(means[-1] - 200), 2)
        time.sleep(0.3)
        self.assertEqual(ns["_mjpeg_viewers"], 0)


if __name__ == "__main__":
    unittest.main()
