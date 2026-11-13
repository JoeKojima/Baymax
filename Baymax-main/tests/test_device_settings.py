"""Microphone / camera / volume from the Ember app (features/device_settings.py), offline."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from features import device_settings as ds
from features.toolkits.everyday.store import Store
from features.toolkits.family.link import Link
from features.toolkits.family.store import FamilyStore


class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "device_settings.json"
        self.volumes = []
        self.settings = self.make()

    def make(self, ok=True):
        return ds.Settings(self.path, volume_setter=lambda v: self.volumes.append(v) or ok)

    def test_defaults_on(self):
        self.assertTrue(self.settings.mic_on())
        self.assertTrue(self.settings.camera_on())
        self.assertTrue(self.settings.camera_wanted.is_set())
        self.assertIsNone(self.settings.report()["volume"])

    def test_apply_newer_only(self):
        self.assertEqual(self.settings.apply({"rev": 2, "mic": False, "camera": False, "volume": 40, "changed_by": "Jane"}),
                         ["mic", "camera", "volume"])
        self.assertFalse(self.settings.mic_on())
        self.assertFalse(self.settings.camera_wanted.is_set())
        self.assertEqual(self.volumes, [40])
        # Same or older revision: ignored (the inbox repeats it every poll).
        self.assertEqual(self.settings.apply({"rev": 2, "mic": True}), [])
        self.assertEqual(self.settings.apply({"rev": 1, "camera": True}), [])
        self.assertFalse(self.settings.camera_on())
        self.assertEqual(self.settings.report(), {"rev": 2, "mic": False, "camera": False, "volume": 40, "volume_ok": True, "reports": True, "report_flags": True, "report_time": "20:00"})

    def test_bad_input_ignored(self):
        for remote in (None, "x", {"rev": "nope"}, {"rev": 0, "mic": False}):
            self.assertEqual(self.settings.apply(remote), [])
        self.assertEqual(self.settings.apply({"rev": 1, "mic": "off", "volume": True}), [])
        self.assertTrue(self.settings.mic_on())

    def test_volume_clamped_never_silent(self):
        self.settings.apply({"rev": 1, "volume": 0})
        self.settings.apply({"rev": 2, "volume": 250})
        self.assertEqual(self.volumes, [ds.MIN_VOLUME, ds.MAX_VOLUME])

    def test_volume_retried_when_it_failed(self):
        failing = self.make(ok=False)
        failing.apply({"rev": 1, "volume": 50})
        self.assertFalse(failing.report()["volume_ok"])
        failing.set_volume = lambda v: self.volumes.append(v) or True
        failing.apply({"rev": 2, "volume": 50})
        self.assertTrue(failing.report()["volume_ok"])

    def test_off_survives_restart_and_volume_restored(self):
        self.settings.apply({"rev": 3, "camera": False, "mic": False, "volume": 65})
        again = self.make()
        self.assertFalse(again.camera_on())
        self.assertFalse(again.mic_on())
        self.assertFalse(again.camera_wanted.is_set())
        self.volumes.clear()
        again.start()
        self.assertEqual(self.volumes, [65])

    def test_wait_for_camera(self):
        self.settings.apply({"rev": 1, "camera": False})
        stop = threading.Event()
        threading.Timer(0.05, lambda: self.settings.apply({"rev": 2, "camera": True})).start()
        self.assertTrue(self.settings.wait_for_camera(stop, poll=0.01))
        self.settings.apply({"rev": 3, "camera": False})
        stop.set()
        self.assertFalse(self.settings.wait_for_camera(stop, poll=0.01))

    def test_report_switches_and_time(self):
        self.assertTrue(self.settings.reports_on())
        self.assertTrue(self.settings.report_flags_on())
        self.assertEqual(self.settings.report_time(), (20, 0))
        self.assertEqual(self.settings.apply({"rev": 1, "report_flags": False, "report_time": "07:30"}),
                         ["report_flags", "report_time"])
        self.assertFalse(self.settings.report_flags_on())
        self.assertEqual(self.settings.report_time(), (7, 30))
        self.settings.apply({"rev": 2, "report_time": "25:00", "reports": False, "report_flags": True})
        self.assertEqual(self.settings.report_time(), (7, 30))      # invalid time ignored
        self.assertFalse(self.settings.reports_on())
        self.assertFalse(self.settings.report_flags_on())            # no flags without reports
        self.assertEqual(self.settings.take_announcements(), [])    # not spoken
        self.assertFalse(self.make().reports_on())                   # saved

    def test_nothing_to_send_only_when_mic_and_camera_are_off(self):
        self.assertFalse(self.settings.nothing_to_send())
        self.settings.apply({"rev": 1, "mic": False})
        self.assertFalse(self.settings.nothing_to_send())   # camera frames still keep Gemini busy
        self.settings.apply({"rev": 2, "camera": False})
        self.assertTrue(self.settings.nothing_to_send())

    def test_announcements(self):
        self.settings.apply({"rev": 1, "camera": False, "changed_by": "Jane"})
        self.settings.apply({"rev": 2, "camera": True, "volume": 30, "changed_by": "Sam"})
        self.assertEqual(self.settings.take_announcements(), [("camera", False, "Jane"), ("camera", True, "Sam")])
        self.assertEqual(self.settings.take_announcements(), [])


class SessionTaskTests(unittest.TestCase):
    def run_task(self, settings, can_speak=lambda: True, seconds=1.2):
        session = SimpleNamespace(send_client_content=AsyncMock())
        stop = asyncio.Event()

        async def go():
            old = ds.settings
            ds.settings = settings
            try:
                task = asyncio.create_task(ds.session_task(session, stop, can_speak=can_speak))
                await asyncio.sleep(seconds)
                stop.set()
                await asyncio.wait_for(task, 2)
            finally:
                ds.settings = old
        asyncio.run(go())
        return [(c.kwargs["turns"]["parts"][0]["text"], c.kwargs["turn_complete"])
                for c in session.send_client_content.call_args_list]

    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return ds.Settings(Path(tmp.name) / "s.json", volume_setter=lambda v: True)

    def test_off_at_start_tells_gemini_quietly(self):
        settings = self.make()
        settings.apply({"rev": 1, "camera": False, "mic": False})
        settings.take_announcements()
        sent = self.run_task(settings, seconds=0.1)
        self.assertEqual(len(sent), 1)
        self.assertIn("[CAMERA OFF]", sent[0][0])
        self.assertIn("[MICROPHONE OFF]", sent[0][0])
        self.assertFalse(sent[0][1])

    def test_change_is_said_out_loud_latest_only(self):
        settings = self.make()
        settings.apply({"rev": 1, "camera": False, "changed_by": "Jane"})
        settings.apply({"rev": 2, "camera": True, "changed_by": "Jane"})
        sent = self.run_task(settings)
        self.assertEqual(len(sent), 1)
        self.assertIn("Jane turned your camera back on", sent[0][0])
        self.assertNotIn("turned your camera off", sent[0][0])
        self.assertTrue(sent[0][1])

    def test_waits_until_ember_can_speak(self):
        settings = self.make()
        settings.apply({"rev": 1, "mic": False, "changed_by": "Sam"})
        sent = self.run_task(settings, can_speak=lambda: False)
        self.assertEqual([text for text, _ in sent if "[SETTINGS]" in text], [])
        self.assertEqual(len(settings.announcements), 1)


class LinkSettingsTests(unittest.TestCase):
    def test_inbox_settings_applied_and_reported(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        settings = ds.Settings(root / "s.json", volume_setter=lambda v: True)
        syncs = []
        inbox = {"poll_seconds": 15, "settings": {"rev": 4, "mic": True, "camera": False, "volume": 55, "changed_by": "Jane"}}
        http = SimpleNamespace(
            get=lambda url, headers, timeout: SimpleNamespace(raise_for_status=lambda: None, json=lambda: inbox),
            post=lambda url, json, headers, timeout: syncs.append(json) or SimpleNamespace(raise_for_status=lambda: None, json=dict))
        link = Link(FamilyStore(root / "f.sqlite3"), Store(root / "e.sqlite3", tz="UTC"),
                    url="https://example.test", key="002.k", summarizer=lambda n: n, http=http, settings=settings)
        link.cycle()
        self.assertFalse(settings.camera_on())
        self.assertEqual(syncs[0]["settings"], {"rev": 4, "mic": True, "camera": False, "volume": 55, "volume_ok": True, "reports": True, "report_flags": True, "report_time": "20:00"})


class MicOffTests(unittest.TestCase):
    def test_capture_block_reaches_nothing(self):
        import queue
        import time
        import numpy as np
        import test_public_core_integration as core
        from unittest.mock import Mock, patch
        recordings, microphone, cue = queue.Queue(), asyncio.Queue(), Mock()
        samples = np.full((1024, 1), 1500, dtype=np.int16)

        class Stream:
            latency = .02
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
            def __enter__(self):
                self.callback(samples, 1024, None, None)
            def __exit__(self, *args):
                pass
        namespace = {"asyncio": asyncio, "np": np, "queue": queue, "time": time,
                     "_audio_enhancer": None, "_waiting_cue": cue, "INPUT_CHANNELS": 1,
                     "_audio_record_queue": recordings, "audio_queue_mic": microphone,
                     "INTERRUPT_RMS_THRESHOLD": 1000, "sd": SimpleNamespace(InputStream=Stream),
                     "SEND_SAMPLE_RATE": 48000, "CHUNK_SIZE": 1024,
                     "device_settings": SimpleNamespace(mic_on=lambda: False)}
        v10 = Path(__file__).resolve().parents[1] / "realtime_gemini_10.py"
        with patch.object(core, "CORE", v10):
            listen = core.load_function("listen_audio", namespace)

        async def go():
            try:
                await asyncio.wait_for(listen(), .02)
            except TimeoutError:
                pass
        asyncio.run(go())
        cue.microphone.assert_not_called()
        self.assertTrue(recordings.empty())
        self.assertTrue(microphone.empty())


class RuntimeWiringTests(unittest.TestCase):
    """The v10 runtime can't be imported offline; check the switches are in the hot paths."""
    source = (Path(__file__).resolve().parents[1] / "realtime_gemini_10.py").read_text()

    def test_mic_dropped_first_in_capture_callback(self):
        callback = self.source.split("def audio_callback(indata, frames, time_info, status):", 1)[1]
        self.assertTrue(callback.lstrip().startswith("if not device_settings.mic_on():\n            return"))

    def test_stays_disconnected_while_mic_and_camera_are_off(self):
        loop = self.source.split("async def run():", 1)[1]
        self.assertLess(loop.index("await _stay_disconnected()"), loop.index("client.aio.live.connect("))
        wait = self.source.split("async def _stay_disconnected():", 1)[1].split("async def run():")[0]
        self.assertIn("while not _shutdown_event.is_set() and device_settings.nothing_to_send():", wait)
        self.assertIn("public_toolkit.has_something_to_say", wait)

    def test_fresh_connection_gets_the_recap(self):
        self.assertIn("conversation_session.note_turn(self.speaker, text)", self.source)
        connect = self.source.split("conversation_session.connected()", 1)[1]
        self.assertLess(connect.index("conversation_session.recap()"), connect.index("asyncio.TaskGroup()"))

    def test_camera_loop_and_video_follow_setting(self):
        self.assertIn("while not _shutdown_event.is_set() and device_settings.camera_on():", self.source)
        self.assertIn("frame = _latest_frame if device_settings.camera_on() else None", self.source)
        self.assertIn("while device_settings.wait_for_camera(_shutdown_event):", self.source)


if __name__ == "__main__":
    unittest.main()
