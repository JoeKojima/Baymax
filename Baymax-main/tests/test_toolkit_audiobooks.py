"""Audiobooks mixed into Ember's speaker stream; fake library/decoder, no network or devices."""
import os
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


class FakeListener:
    def __init__(self, on_wake):
        self.on_wake, self.ready, self.failed, self.fed, self.cleared = on_wake, True, False, [], 0

    def start(self):
        pass

    def feed(self, samples):
        self.fed.append(samples)

    def clear(self):
        self.cleared += 1


class AudiobookTests(unittest.TestCase):
    def setUp(self):
        self.library = FakeLibrary()
        self.notes = []
        self.player = Player(source=self.library, decode=chapters, volume=0.5, boost=1.0,
                             listener_factory=FakeListener, notify=self.notes.append)
        self.addCleanup(self.player.execute, {"action": "stop"})

    def play(self):
        result = self.player.execute({"action": "play", "title": "alice in wonderland"})
        self.assertTrue(wait_for(lambda: len(self.player.buffer) >= 9600))
        return result

    def test_play_streams_chapters_at_volume_and_holds_while_ember_speaks(self):
        result = self.play()
        self.assertEqual({k: result[k] for k in ("status", "title", "author", "chapters", "wake_words")},
                         {"status": "starting", "title": "Alice", "author": "Carroll", "chapters": 2, "wake_words": "ready"})
        self.assertIn("LibriVox", result["recording"])  # Ember tells people it is a recording...
        self.assertIn("Hey Ember", result["how_to_pause"])  # ...and how to stop it.
        self.assertIsNone(self.player.render(2048, voice_active=True))  # Ember talking: book holds.
        held = len(self.player.buffer)
        self.assertEqual(len(self.player.buffer), held)
        pcm = np.frombuffer(self.player.render(2048, voice_active=False), dtype=np.int16)
        self.assertTrue(np.all(pcm == 500))  # 1000 at volume 0.5.
        status = self.player.execute({"action": "status"})
        self.assertEqual((status["status"], status["chapter"], status["chapter_title"]), ("playing", 1, "Rabbit Hole"))
        self.assertEqual(status["volume"], 50)
        louder = self.player.execute({"action": "volume", "level": 80})  # "Make it louder."
        self.assertEqual((louder["volume"], louder["status"]), (80, "playing"))
        ramp = np.frombuffer(self.player.render(2048, voice_active=False), dtype=np.int16)
        self.assertEqual((ramp[0], ramp[-1]), (500, 800))  # Volume changes ramp: no click.
        pcm = np.frombuffer(self.player.render(2048, voice_active=False), dtype=np.int16)
        self.assertTrue(np.all(pcm == 800))
        self.assertEqual(self.player.execute({"action": "volume", "level": 250})["volume"], 100)
        for bad in ({"action": "volume"}, {"action": "volume", "level": "loud"},
                    {"action": "volume", "level": float("nan")}):
            self.assertIn("error", self.player.execute(bad))

    def test_default_boost_is_25_percent_louder_and_clips_instead_of_wrapping(self):
        self.assertEqual(audiobooks.BOOST, 1.25)
        with patch.dict(os.environ, {"BAYMAX_BOOK_BOOST": "loud"}):
            self.assertEqual(audiobooks._boost(), 1.25)
        for boost, expected in ((1.25, 1250), (40.0, 32767)):  # chapters are a steady 1000
            self.player.execute({"action": "stop"})
            self.player = Player(source=self.library, decode=chapters, volume=1.0, boost=boost,
                                 listener_factory=FakeListener, notify=self.notes.append)
            self.play()
            pcm = np.frombuffer(self.player.render(2048, voice_active=False), dtype=np.int16)
            self.assertTrue(np.all(pcm == expected), (boost, pcm[:3]))
        self.assertEqual(self.player.execute({"action": "status"})["volume"], 100)

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

    def drain(self, samples):
        def step():
            chunk = self.player.render(4096, False)
            if chunk:
                samples.append(np.frombuffer(chunk, dtype=np.int16))
            return self.player.paused or not self.player.playing
        return wait_for(step, timeout=5)

    def test_chapter_end_pauses_tells_ember_then_book_finishes(self):
        self.play()
        first, second = [], []
        self.assertTrue(self.drain(first))
        # Stops exactly at the boundary: chapter 2 never leaks in (the last chunk is padded with silence).
        self.assertEqual(set(np.unique(np.concatenate(first))) - {0}, {500})
        self.assertTrue(self.player.paused)
        self.assertIn("Chapter 1 ('Rabbit Hole') of Alice by Carroll just finished", self.notes[-1])
        self.assertEqual(self.player.execute({"action": "status"})["chapter"], 1)
        self.player.execute({"action": "resume"})
        self.assertTrue(self.drain(second))
        self.assertEqual(set(np.unique(np.concatenate(second))) - {0}, {1000})
        self.assertFalse(self.player.playing)
        self.assertIn("Alice is finished", self.notes[-1])
        self.assertEqual(self.library.downloads, [0, 1])
        self.assertEqual(self.player.execute({"action": "status"}), {"status": "stopped"})

    def test_book_mode_withholds_mic_and_wake_word_pauses_with_context(self):
        mic = np.full(480, 7, dtype=np.int16)
        self.assertFalse(self.player.gate(mic))  # No book: mic goes to Gemini.
        self.play()
        listener = self.player.listener
        self.assertTrue(self.player.gate(mic))  # Book: withheld, but heard by the wake listener.
        self.assertIs(listener.fed[-1], mic)
        listener.ready = False
        self.assertFalse(self.player.gate(mic))  # No wake model: fall back to the open mic.
        listener.ready = True
        listener.on_wake("hey ember")
        self.assertTrue(self.player.paused)
        self.assertIn("said 'hey ember'", self.notes[-1])
        self.assertIn("partway through chapter 1 ('Rabbit Hole') of Alice", self.notes[-1])
        self.assertFalse(self.player.gate(mic))  # Paused: talk to Ember normally.
        listener.on_wake("stop")
        self.assertEqual(len(self.notes), 1)  # Already paused: no second note.
        self.player.execute({"action": "resume"})
        self.assertEqual(listener.cleared, 1)
        self.assertTrue(self.player.gate(mic))

    def test_ember_is_told_through_the_live_session_loop(self):
        import asyncio
        sent = []
        class Session:
            async def send_client_content(self, **kwargs):
                sent.append(kwargs)
        async def run():
            audiobooks.attach(Session(), asyncio.get_running_loop())
            await asyncio.to_thread(audiobooks.tell_gemini, "[AUDIOBOOK] paused")
            await asyncio.sleep(0.05)
        try:
            asyncio.run(run())
        finally:
            audiobooks.attach(None, None)
        self.assertEqual(sent, [{"turns": {"role": "user", "parts": [{"text": "[AUDIOBOOK] paused"}]},
                                 "turn_complete": True}])
        audiobooks.tell_gemini("no session")  # Without a session it only logs.

    def test_invalid_requests(self):
        self.assertIn("error", self.player.execute({"action": "dance"}))
        self.assertIn("error", self.player.execute(None))
        self.assertIn("error", self.player.execute({"action": "play"}))
        self.assertIn("error", self.player.execute({"action": "pause"}))
        missing = Player(source=FakeLibrary(found=False), decode=chapters, listener_factory=FakeListener)
        self.assertIn("error", missing.execute({"action": "play", "title": "Harry Potter"}))
        with patch.dict("sys.modules", {"miniaudio": None}):
            self.assertIn("not installed", Player(source=self.library, listener_factory=FakeListener)
                          .execute({"action": "play", "title": "x"})["error"])

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

    def test_mic_gate_in_audio_cleanup(self):
        calls = []
        def gate(samples):
            calls.append(samples)
            return True
        with patch("features.conversational_ability.processing.input_gates", [gate]), \
                patch.dict("os.environ", {"BAYMAX_MUTE_MIC_DURING_REPLY": "1"}):
            enhancer = InputEnhancer(processor=FakeDSP())
            near = np.full(960, 1000, dtype=np.int16)
            self.assertFalse(np.any(enhancer.process(near)))
            self.assertTrue(enhancer.metrics["mic_withheld_for_audiobook"])
            self.assertTrue(np.any(calls[-1]))  # The wake listener gets the real (cleaned) audio.
            enhancer.rendered(np.full(1024, 300, dtype=np.int16).tobytes())
            count = len(calls)
            enhancer.process(near)
            self.assertEqual(len(calls), count)  # Ember speaking: muted anyway, gate not consulted.

    def test_registered_with_toolkit_and_speaker(self):
        from features.conversational_ability.processing import input_gates
        self.assertIn(audiobooks.player.gate, input_gates)
        self.assertIn(audiobooks.player.render, output_sources)
        self.assertIn("audiobook", [d["name"] for d in gemini_tools.DECLARATIONS])
        self.assertIn("[AUDIOBOOKS]", gemini_tools.INSTRUCTION)
        with patch.object(audiobooks, "execute", return_value={"status": "stopped"}) as run, \
                patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1"}):
            self.assertEqual(gemini_tools.execute("audiobook", {"action": "status"}), {"status": "stopped"})
        run.assert_called_once_with({"action": "status"})


class WakeWordTests(unittest.TestCase):
    def test_phrase_rules(self):
        from features.toolkits.wakeword import heard
        def words(*pairs):
            return {"result": [{"word": w, "conf": c} for w, c in pairs]}
        # Real phrases (measured: every word at 1.0).
        self.assertEqual(heard(words(("hey", 1.0), ("ember", 1.0)), True), "hey ember")
        self.assertEqual(heard(words(("ember", 1.0), ("pause", 1.0)), True), "ember pause")
        self.assertEqual(heard(words(("[unk]", 1.0), ("ember", 0.95), ("stop", 0.97)), True), "ember stop")
        self.assertEqual(heard(words(("stop", 1.0)), True), "stop")
        self.assertEqual(heard(words(("pause", 0.92)), True), "pause")
        # What narration produced: partial guesses, lone "ember" (even at 1.0), weak "hey".
        self.assertIsNone(heard({"partial": "[unk] hey ember"}, False))
        self.assertIsNone(heard(words(("ember", 1.0)), True))
        self.assertIsNone(heard(words(("hey", 0.68)), True))
        self.assertIsNone(heard(words(("hey", 0.5), ("ember", 1.0)), True))
        self.assertIsNone(heard(words(("stop", 0.6)), True))
        self.assertIsNone(heard(words(("ember", 1.0), ("[unk]", 1.0), ("stop", 0.5)), True))
        self.assertEqual(heard(words(("[unk]", 1.0), ("stop", 1.0)), True), "stop")  # "please stop"
        # Measured false hit: "pause" mid-narration.
        self.assertIsNone(heard(words(("[unk]", 1.0), ("[unk]", 1.0), ("pause", 1.0), ("[unk]", 1.0)), True))
        self.assertIsNone(heard(words(("pause", 1.0), ("[unk]", 1.0)), True))

    def test_attempts_missed_on_ember_002_now_wake(self):
        """Real robot results (2026-10-22) that used to be rejected while someone tried to stop the book."""
        from features.toolkits.wakeword import heard
        def timed(start, *pairs):
            return {"result": [{"word": w, "conf": c, "start": start + i * 0.5, "end": start + i * 0.5 + 0.4}
                               for i, (w, c) in enumerate(pairs)]}
        self.assertEqual(heard(timed(0, ("ember", 1.0), ("hey", 1.0)), True), "ember hey")      # either order
        self.assertEqual(heard(timed(0, ("[unk]", 1.0), ("stop", 0.88), ("hey", 0.88)), True), "stop hey")
        # Vosk ended the utterance between the words: hey(1.00), then "_ ember(0.85)" 2 s later.
        first = timed(0, ("hey", 1.0))
        self.assertIsNone(heard(first, True))
        self.assertEqual(heard(timed(2.0, ("[unk]", 1.0), ("ember", 0.85)), True, previous=first), "hey ember")
        # ...but not when the halves are far apart, the same word twice, or unsure.
        self.assertIsNone(heard(timed(9.0, ("ember", 1.0)), True, previous=first))
        self.assertIsNone(heard(timed(0, ("okay", 0.87), ("okay", 1.0)), True))
        self.assertIsNone(heard(timed(0, ("hey", 0.71), ("ember", 1.0)), True))
        self.assertIsNone(heard(timed(0, ("ember", 1.0)), True))                               # still never alone

    def test_listener_hints_on_a_lone_wake_word_and_the_book_ducks(self):
        import json
        from features.toolkits.wakeword import WakeListener
        results = iter([[{"word": "[unk]", "conf": 1.0, "start": 0.0, "end": 0.01},
                         {"word": "hey", "conf": 0.9, "start": 0.01, "end": 0.02}],
                        [{"word": "[unk]", "conf": 1.0, "start": 0.03, "end": 0.04}]])
        class Recognizer:
            def AcceptWaveform(self, pcm):
                return True
            def Result(self):
                return json.dumps({"result": next(results, [])})
        hints, woke = [], []
        listener = WakeListener(woke.append, recognizer_factory=Recognizer, on_hint=lambda: hints.append(1))
        listener.start()
        self.assertTrue(wait_for(lambda: listener.ready))
        listener.feed(np.ones(1024, dtype=np.int16))
        listener.feed(np.ones(1024, dtype=np.int16))
        self.assertTrue(wait_for(lambda: hints == [1]))
        self.assertEqual(woke, [])

        clock = [100.0]
        player = Player(source=FakeLibrary(), decode=chapters, volume=1.0, boost=1.0,
                        listener_factory=FakeListener, notify=lambda text: None, clock=lambda: clock[0])
        self.addCleanup(player.execute, {"action": "stop"})
        player.execute({"action": "play", "title": "alice in wonderland"})
        self.assertTrue(wait_for(lambda: len(player.buffer) >= 9600))
        self.assertEqual(player.listener.on_hint, player.duck)
        self.assertTrue(np.all(np.frombuffer(player.render(2048, False), dtype=np.int16) == 1000))
        player.duck()
        ramp = np.frombuffer(player.render(2048, False), dtype=np.int16)
        self.assertEqual((ramp[0], ramp[-1]), (1000, 300))                  # ramps down, no click
        self.assertTrue(np.all(np.frombuffer(player.render(2048, False), dtype=np.int16) == 300))
        clock[0] += 4.1
        ramp = np.frombuffer(player.render(2048, False), dtype=np.int16)
        self.assertEqual((ramp[0], ramp[-1]), (300, 1000))                  # back up after 4 s

    def test_listener_downsamples_and_wakes(self):
        import json
        from features.toolkits.wakeword import WakeListener
        class Recognizer:
            def __init__(self):
                self.bytes = 0
            def AcceptWaveform(self, pcm):
                self.bytes += len(pcm)
                return self.bytes >= 3200 * 2  # Final after 0.2 s at 16 kHz.
            def Result(self):
                return json.dumps({"text": "hey ember", "result": [{"word": "hey", "conf": 1.0},
                                                                    {"word": "ember", "conf": 1.0}]})
            def PartialResult(self):
                return json.dumps({"partial": ""})
            def Reset(self):
                pass
        recognizer, woke = Recognizer(), []
        listener = WakeListener(woke.append, recognizer_factory=lambda: recognizer)
        listener.start()
        self.assertTrue(wait_for(lambda: listener.ready))
        for _ in range(10):
            listener.feed(np.zeros(1024, dtype=np.int16))  # 10 x 1024 samples at 48 kHz.
        self.assertTrue(wait_for(lambda: woke == ["hey ember"]))
        # Every 3rd sample of 10 x 1024 at 48 kHz, phase kept across blocks: 3414 samples at 16 kHz.
        self.assertEqual(recognizer.bytes, 3414 * 2)
        self.assertEqual(listener.fed16, 3414)

    def test_lone_stop_counts_when_louder_than_the_narration(self):
        from features.toolkits.wakeword import heard
        result = {"result": [{"word": "[unk]", "conf": 1.0, "start": 1.0, "end": 2.0},
                             {"word": "stop", "conf": 1.0, "start": 2.1, "end": 2.5},
                             {"word": "[unk]", "conf": 1.0, "start": 2.6, "end": 3.5}]}
        measured = {1.0: 42.0, 2.1: 70.0, 2.6: 44.0}  # dB measured on ember-002-like audio.
        self.assertEqual(heard(result, True, lambda start, end: measured[start]), "stop")
        measured[2.1] = 50.0  # Narrator's own "stop": barely louder.
        self.assertIsNone(heard(result, True, lambda start, end: measured[start]))
        self.assertIsNone(heard(result, True))  # Without loudness info, mid-sentence never counts.

    def test_level_maps_recognizer_time_to_recent_audio(self):
        from features.toolkits.wakeword import WakeListener
        listener = WakeListener(lambda phrase: None)
        loud = np.concatenate([np.zeros(48000), np.full(48000, 3000.0), np.zeros(48000)])
        listener._to_16k(loud)
        self.assertGreater(listener.level(1.2, 1.8), listener.level(0.2, 0.8) + 40)
        self.assertEqual(listener.level(100, 101), 0.0)  # Outside the history: harmless.

    def test_missing_model_or_library_leaves_mic_open(self):
        from features.toolkits.wakeword import WakeListener
        listener = WakeListener(lambda phrase: None, recognizer_factory=lambda: (_ for _ in ()).throw(ImportError("vosk")))
        listener.start()
        self.assertTrue(wait_for(lambda: listener.failed))
        self.assertFalse(listener.ready)
        listener.feed(np.zeros(1024, dtype=np.int16))  # Ignored, never raises.


if __name__ == "__main__":
    unittest.main()
