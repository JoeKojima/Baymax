"""Virtual devices, the fake Gemini and the legacy-core loader (no hardware, no network)."""
import asyncio
import os
import unittest
from unittest.mock import patch

import numpy as np

from ember.devices.virtual import MicSource, VirtualCamera, VirtualSoundDevice
from ember.testing import fake_gemini as fg
from ember.testing import legacy


class VirtualDeviceTests(unittest.TestCase):
    def test_microphone_plays_queued_lines_then_background(self):
        mic = MicSource()
        mic.say(np.arange(1, 1501, dtype=np.int16))
        first, second = mic.read(1024), mic.read(1024)
        self.assertEqual(first.tolist(), list(range(1, 1025)))
        self.assertEqual(second[:476].tolist(), list(range(1025, 1501)))
        self.assertFalse(np.any(second[476:]))
        noisy = MicSource(room_noise=200).read(48000)
        self.assertTrue(150 < noisy.std() < 250)

    def test_manual_streams_run_callbacks_and_record_the_speaker(self):
        sd = VirtualSoundDevice(realtime=False)
        heard = []
        mic = sd.InputStream(samplerate=48000, channels=2, dtype="int16", blocksize=1024,
                             callback=lambda indata, frames, timing, status: heard.append(indata.copy()))
        speaker = sd.OutputStream(samplerate=48000, channels=1, dtype="int16", blocksize=1024,
                                  callback=lambda outdata, frames, timing, status: outdata.fill(7))
        sd.microphone.say(np.full(1024, 5, dtype=np.int16))
        with mic:
            speaker.start()
            sd.step(2)
            speaker.close()
        self.assertEqual(heard[0].shape, (1024, 2))
        self.assertTrue(np.all(heard[0] == 5))
        self.assertEqual(sd.speaker.voiced_blocks, 2)
        self.assertEqual(len(sd.speaker.take()), 2 * 1024 * 2)
        self.assertEqual(sd.open_streams(), [])

    def test_camera_loops_frames_and_can_show_a_card(self):
        camera = VirtualCamera([np.zeros((2, 2, 3), np.uint8), np.ones((2, 2, 3), np.uint8)])
        self.assertEqual([int(camera.read()[1][0, 0, 0]) for _ in range(3)], [0, 1, 0])
        camera.show(np.full((2, 2, 3), 9, np.uint8))
        self.assertEqual(int(camera.read()[1][0, 0, 0]), 9)
        camera.release()
        self.assertEqual(camera.read(), (False, None))


class FakeGeminiTests(unittest.TestCase):
    def test_rules_reply_and_receive_ends_at_turn_complete_like_genai(self):
        script = fg.Script(fg.Rule([fg.content(heard="Hi"), fg.content(said="Hello", complete=True)], audio=10),
                           fg.Rule([fg.drop(1011)], text="bye"))
        client = fg.FakeClient(script, max_connects=1)

        async def go():
            async with client.aio.live.connect(model="m", config={"a": 1}) as session:
                await session.send_realtime_input(audio=fg.blob(b"\0" * 10, "audio/pcm;rate=48000"))
                turn = [m async for m in session.receive()]
                await session.send_client_content(turns={"role": "user", "parts": [{"text": "bye"}]})
                with self.assertRaises(fg.Closed):
                    [m async for m in session.receive()]
                with self.assertRaises(fg.Closed):
                    await session.send_realtime_input(audio=fg.blob(b"\0", "x"))
            return turn
        turn = asyncio.run(go())
        self.assertEqual([m.server_content.turn_complete for m in turn], [False, True])
        self.assertEqual(client.connects, [{"model": "m", "config": {"a": 1}}])
        with self.assertRaises(fg.StopRuntime):
            asyncio.run(client.aio.live.connect(model="m", config={}).__aenter__())


class LegacyLoaderTests(unittest.TestCase):
    def test_unchanged_v8_loads_on_virtual_devices_with_fresh_globals(self):
        sd = VirtualSoundDevice(realtime=False)
        with patch.dict(os.environ, {"BAYMAX_AUDIO_ENHANCEMENT": "1"}):
            first = legacy.load(sd)
            second = legacy.load(sd)
        self.assertEqual((first.target_device_microphone, first.target_device_speaker), (0, 1))
        self.assertIsNotNone(first._audio_enhancer)
        first._append_playback(b"\1\1")
        self.assertEqual(second._playback_buffer, b"")
        self.assertNotIn("sounddevice", {name for name, module in __import__("sys").modules.items() if module is sd})

    def test_camera_context_blocks_real_cameras(self):
        import cv2
        with legacy.camera(None):
            with self.assertRaises(legacy.RealCameraBlocked):
                cv2.VideoCapture(0)


if __name__ == "__main__":
    unittest.main()
