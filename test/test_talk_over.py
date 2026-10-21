"""
Tests for talk_over.py (pausing an audiobook when someone talks over it).

    cd test && python3 -m unittest test_talk_over -v

A simulated room, sized from unit 001's log (book echo ~530 RMS median, the
user's voice ~1,100): the book writes 0.1 s chunks of speech-like audio, the
mic hears them at ECHO_GAIN about ECHO_DELAY later, with a little smearing
from the room and some noise, and a person can talk on top.
"""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Baymax-main"))
import talk_over as to  # noqa: E402

RATE = 48000
BLOCK = 1024 / RATE         # one mic callback
CHUNK = 0.1                 # one book write
STEP = 0.02                 # syllable-level detail of the narration
ECHO_GAIN = 0.35
ECHO_DELAY = 0.23
NOISE = 25


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Room:
    def __init__(self, ratio=to.DEFAULT_RATIO, seed=1, echo_gain=ECHO_GAIN, echo_delay=ECHO_DELAY):
        self.clock = Clock()
        self.logs = []
        self.det = to.TalkOverDetector(ratio=ratio, clock=self.clock, log=self.logs.append)
        self.rand = random.Random(seed)
        self.np_rand = np.random.default_rng(seed)
        self.echo_gain, self.echo_delay = echo_gain, echo_delay
        self.played = []            # (time, rms) per STEP of book audio
        self.next_chunk = self.clock.t
        self.level = 1500.0
        self.syllable = (0.0, 1.0)      # (until, loudness factor) of the talker's current syllable
        self.starts = 0

    def _narration_step(self):
        # Speech-like loudness: syllables rising and falling, short pauses.
        if self.rand.random() < 0.06:
            self.level = 0.0
        else:
            target = self.rand.choice([600, 1200, 1800, 2600, 3400])
            self.level = 0.6 * self.level + 0.4 * target
        return self.level

    def _echo_at(self, t):
        # The room smears the echo over ~3 steps.
        total, weight = 0.0, 0.0
        for when, rms in reversed(self.played):
            age = t - self.echo_delay - when
            if age < 0:
                continue
            if age > 3 * STEP:
                break
            w = 1.0 - age / (3 * STEP)
            total += w * rms ** 2
            weight += w
        return self.echo_gain * (total / weight) ** 0.5 if weight else 0.0

    def run(self, seconds, voice=0.0, book=True):
        """Advance time; voice is the RMS of a person talking (0 = silent)."""
        end = self.clock.t + seconds
        while self.clock.t < end:
            if book and self.clock.t >= self.next_chunk:
                if not self.det.holding():          # a held book writes nothing
                    parts = []
                    for i in range(int(CHUNK / STEP)):
                        rms = self._narration_step()
                        self.played.append((self.clock.t + i * STEP, rms))
                        parts.append(self.np_rand.normal(0, rms, int(STEP * RATE)))
                    self.det.book_level(np.concatenate(parts))
                self.next_chunk += CHUNK
            echo = self._echo_at(self.clock.t)
            if voice and self.clock.t >= self.syllable[0]:
                # Speech: syllables of ~60-200 ms, louder and softer, short gaps.
                factor = 0.0 if self.rand.random() < 0.15 else self.rand.uniform(0.5, 1.4)
                self.syllable = (self.clock.t + self.rand.uniform(0.06, 0.2), factor)
            spoken = voice * self.syllable[1]
            mic = (echo ** 2 + spoken ** 2 + NOISE ** 2) ** 0.5 * self.rand.uniform(0.85, 1.15)
            playing = book and not self.det.holding()
            if playing or self.det.holding():
                self.starts += self.det.mic_level(mic, BLOCK)
            self.clock.t += BLOCK


class TalkOverTests(unittest.TestCase):
    def test_book_alone_never_pauses(self):
        for seed in range(5):
            with self.subTest(seed=seed):
                room = Room(seed=seed)
                room.run(120)
                self.assertEqual(room.starts, 0, room.logs[-3:])

    def test_finds_the_rooms_delay_and_gain(self):
        room = Room()
        room.run(10)
        self.assertAlmostEqual(room.det._delay, ECHO_DELAY, delta=0.05)
        self.assertAlmostEqual(room.det._gain, ECHO_GAIN, delta=0.1)

    def test_talking_over_the_book_pauses_it_quickly(self):
        for voice in (900, 1127, 2000):      # soft, the measured median, loud
            with self.subTest(voice=voice):
                room = Room()
                room.run(6)
                t0 = room.clock.t
                room.run(0.8, voice=voice)   # "Ember, pause the—"
                self.assertTrue(room.det.holding())
                self.assertEqual(room.starts, 1)
                self.assertLess(room.det._hold_until - to.HOLD_SECONDS - t0, 0.8)

    def test_hold_lasts_while_talking_then_book_continues(self):
        room = Room()
        room.run(6)
        room.run(2.0, voice=1200)            # a whole sentence
        self.assertTrue(room.det.holding())
        room.run(to.HOLD_SECONDS - 0.5)      # quiet: Gemini would answer now
        self.assertTrue(room.det.holding())
        room.run(1.0)
        self.assertFalse(room.det.holding())
        self.assertEqual(room.starts, 1)
        self.assertIn("continues", room.logs[-1])

    def test_a_cough_does_not_pause(self):
        room = Room()
        room.run(6)
        room.run(0.1, voice=3000)
        room.run(1)
        self.assertEqual(room.starts, 0)

    def test_nothing_before_it_has_learned_the_room(self):
        room = Room()
        room.run(1.0, voice=2000)
        self.assertEqual(room.starts, 0)

    def test_quiet_sounds_never_count(self):
        room = Room()
        room.run(6)
        room.run(2, voice=to.MIN_TALK_RMS * 0.6)
        self.assertEqual(room.starts, 0)

    def test_louder_room_still_works(self):
        room = Room(echo_gain=0.8, echo_delay=0.4)
        room.run(60)
        self.assertEqual(room.starts, 0)
        room.run(0.8, voice=2500)
        self.assertEqual(room.starts, 1)


if __name__ == "__main__":
    unittest.main()
