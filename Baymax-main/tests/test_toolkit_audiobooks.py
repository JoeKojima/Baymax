"""Audiobooks mixed into Ember's speaker stream; fake library/decoder, no network or devices."""
import time
import unittest
from unittest.mock import patch

import numpy as np

from features.conversational_ability.processing import InputEnhancer, output_sources
from features.toolkits import audiobooks, gemini_tools
from features.toolkits.audiobooks import Player

BOOK = {"id": "alice", "title": "Alice", "author": "Carroll",
        "sections": [{"title": "Rabbit Hole", "file": "1.mp3"}, {"title": "Pool of Tears", "file": "2.mp3"}]}


class FakeLibrary:
    def __init__(self, found=True):
        self.found, self.downloads = found, []

    def search(self, title=None, author=None, limit=5):
        return [{"id": "alice"}] if self.found else []

    def fetch_book(self, identifier):
        return BOOK

    def download(self, book, index, cancelled=lambda: False):
        self.downloads.append(index)
        return index


def chapters(path, rate):
    # Chapter i decodes to three 4800-sample blocks of value (i + 1) * 1000.
    for _ in range(3):
        yield np.full(4800, (path + 1) * 1000, dtype=np.int16)


def wait_for(condition, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


class FakeDSP:
    speech_probability = 0.5
    gain_db = 0

    def reset(self):
        pass

    def process(self, near, far):
        return near.copy()


class AudiobookTests(unittest.TestCase):
    def setUp(self):
        self.library = FakeLibrary()
        self.player = Player(source=self.library, decode=chapters, volume=0.5)
        self.addCleanup(self.player.execute, {"action": "stop"})

    def play(self):
        result = self.player.execute({"action": "play", "title": "alice in wonderland"})
        self.assertTrue(wait_for(lambda: len(self.player.buffer) >= 9600))
        return result

    def test_play_streams_chapters_at_volume_and_holds_while_ember_speaks(self):
        self.assertEqual(self.play(), {"status": "starting", "title": "Alice", "author": "Carroll", "chapters": 2})
        self.assertIsNone(self.player.render(2048, voice_active=True))  # Ember talking: book holds.
        held = len(self.player.buffer)
        self.assertEqual(len(self.player.buffer), held)
        pcm = np.frombuffer(self.player.render(2048, voice_active=False), dtype=np.int16)
        self.assertTrue(np.all(pcm == 500))  # 1000 at volume 0.5.
        status = self.player.execute({"action": "status"})
        self.assertEqual((status["status"], status["chapter"], status["chapter_title"]), ("playing", 1, "Rabbit Hole"))

    def test_pause_resume_next_stop(self):
        self.play()
        self.assertEqual(self.player.execute({"action": "pause"})["status"], "paused")
        self.assertIsNone(self.player.render(2048, False))
        self.assertEqual(self.player.execute({"action": "resume"})["status"], "playing")
        self.assertIsNotNone(self.player.render(2048, False))
        self.assertEqual(self.player.execute({"action": "next"})["chapter"], 2)
        self.assertTrue(wait_for(lambda: len(self.player.buffer) >= 2048))
        self.assertTrue(np.all(np.frombuffer(self.player.render(2048, False), dtype=np.int16) == 1000))
        self.assertIn("error", self.player.execute({"action": "next"}))  # Last chapter.
        self.assertEqual(self.player.execute({"action": "stop"}), {"status": "stopped"})
        self.assertIsNone(self.player.render(2048, False))
        self.assertEqual(self.player.execute({"action": "status"}), {"status": "stopped"})

    def test_whole_book_plays_through_then_stops(self):
        self.play()
        samples = []
        def done():
            chunk = self.player.render(4096, False)
            if chunk:
                samples.append(np.frombuffer(chunk, dtype=np.int16))
            return not self.player.playing
        self.assertTrue(wait_for(done, timeout=5))
        audio = np.concatenate(samples)
        self.assertEqual(set(np.unique(audio)) - {0}, {500, 1000})  # Both chapters, in order.
        self.assertEqual(self.library.downloads, [0, 1])
        self.assertEqual(self.player.execute({"action": "status"}), {"status": "stopped"})

    def test_invalid_requests(self):
        self.assertIn("error", self.player.execute({"action": "dance"}))
        self.assertIn("error", self.player.execute(None))
        self.assertIn("error", self.player.execute({"action": "play"}))
        self.assertIn("error", self.player.execute({"action": "pause"}))
        missing = Player(source=FakeLibrary(found=False), decode=chapters)
        self.assertIn("error", missing.execute({"action": "play", "title": "Harry Potter"}))
        with patch.dict("sys.modules", {"miniaudio": None}):
            self.assertIn("not installed", Player(source=self.library).execute({"action": "play", "title": "x"})["error"])

    def test_book_is_mixed_into_speaker_and_does_not_mute_the_mic(self):
        book = [np.full(1024, 600, dtype=np.int16).tobytes()]
        voice_seen = []
        def source(n_bytes, voice_active):
            voice_seen.append(voice_active)
            return book[0]
        with patch("features.conversational_ability.processing.output_sources", [source]), \
                patch.dict("os.environ", {"BAYMAX_MUTE_MIC_DURING_REPLY": "1"}):
            enhancer = InputEnhancer(processor=FakeDSP())
            silence = bytes(2048)
            mixed = enhancer.mix_output(silence)
            enhancer.rendered(mixed)
            self.assertTrue(np.all(np.frombuffer(mixed, dtype=np.int16) == 600))
            near = np.full(960, 1000, dtype=np.int16)
            self.assertTrue(np.any(enhancer.process(near)))  # Mic still open: you can say "pause".
            self.assertTrue(enhancer.metrics["echo_guard"])  # But echo protection knows the speaker is on.
            voice = np.full(1024, 300, dtype=np.int16).tobytes()
            enhancer.rendered(enhancer.mix_output(voice))
            self.assertFalse(np.any(enhancer.process(near)))  # Ember's own voice still mutes the mic.
        self.assertEqual(voice_seen, [False, True])

    def test_registered_with_toolkit_and_speaker(self):
        self.assertIn(audiobooks.player.render, output_sources)
        self.assertIn("audiobook", [d["name"] for d in gemini_tools.DECLARATIONS])
        self.assertIn("[AUDIOBOOKS]", gemini_tools.INSTRUCTION)
        with patch.object(audiobooks, "execute", return_value={"status": "stopped"}) as run, \
                patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1"}):
            self.assertEqual(gemini_tools.execute("audiobook", {"action": "status"}), {"status": "stopped"})
        run.assert_called_once_with({"action": "status"})


if __name__ == "__main__":
    unittest.main()
