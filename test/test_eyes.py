"""
Tests for Baymax-main/eyes.py — no hardware needed.

A fake pyfirmata2 board records every pin write, so the tests can decode the
exact MAX7219 bitstream and check what each eye is displaying.

    python3 -m unittest test/test_eyes.py -v
"""
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Baymax-main"))


# ─── Fake Firmata board ──────────────────────────────────────────────────────
class _Pin:
    def __init__(self, board, number):
        self.board, self.number = board, number

    def write(self, value):
        self.board.on_write(self.number, value)


class FakeBoard:
    AUTODETECT = "auto"

    def __init__(self, port):
        self.writes = 0
        self.transfers = []          # list of 32-bit words, one per CS pulse
        self.servo = []
        self._din = 0
        self._bits = None
        self._lock = threading.Lock()

    def get_pin(self, spec):
        kind, number, mode = spec.split(":")
        return _Pin(self, (int(number), mode))

    def on_write(self, pin, value):
        number, mode = pin
        with self._lock:
            self.writes += 1
            if mode == "s":
                self.servo.append(value)
            elif number == 6:                      # DIN
                self._din = value
            elif number == 10 and value == 0:      # CS low: start word
                self._bits = []
            elif number == 10 and value == 1:      # CS high: latch word
                if self._bits is not None:
                    self.transfers.append(int("".join(map(str, self._bits)), 2))
                self._bits = None
            elif number == 13 and value == 1 and self._bits is not None:  # CLK rising
                self._bits.append(self._din)

    def displayed(self):
        """Replay all transfers and return (eye1_rows, eye2_rows)."""
        with self._lock:
            words = list(self.transfers)
        rows = {0: [None] * 8, 1: [None] * 8}
        for word in words:
            for device, half in ((1, word >> 16), (0, word & 0xFFFF)):
                opcode, data = half >> 8, half & 0xFF
                if 1 <= opcode <= 8:
                    rows[device][opcode - 1] = data
        return rows[0], rows[1]

    def exit(self):
        pass


sys.modules["pyfirmata2"] = types.SimpleNamespace(Arduino=FakeBoard)
import eyes  # noqa: E402


def _flip(pattern):
    return list(reversed(pattern))


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ─── Patterns ────────────────────────────────────────────────────────────────
class PatternTests(unittest.TestCase):
    def test_open_eye_is_pupil_at_row_3(self):
        self.assertEqual(eyes.eye_with_pupil(3), eyes.EYE_OPEN)

    def test_thinking_frames_are_left_right_symmetric(self):
        # Eye 2 is mounted flipped; symmetric frames look right either way.
        def mirror(byte):
            return int(f"{byte:08b}"[::-1], 2)
        frames = eyes.THINKING_ENTER_FRAMES + eyes.THINKING_LOOP_FRAMES
        for frame in frames:
            self.assertEqual(len(frame), 8)
            self.assertEqual([mirror(r) for r in frame], frame)

    def test_thinking_looks_different_from_open(self):
        for frame in eyes.THINKING_LOOP_FRAMES:
            self.assertNotEqual(frame, eyes.EYE_OPEN)


# ─── MAX7219 driver ──────────────────────────────────────────────────────────
class LedControlTests(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard(None)
        self.lc = eyes.LedControl(self.board, eyes.DIN_PIN, eyes.CLK_PIN,
                                  eyes.CS_PIN, eyes.NUM_DEVICES)

    def test_init_clears_every_row(self):
        eye1, eye2 = self.board.displayed()
        self.assertEqual(eye1, [0] * 8)
        self.assertEqual(eye2, [0] * 8)

    def test_set_row_targets_one_device_with_noop_for_other(self):
        self.board.transfers.clear()
        self.lc.set_row(1, 2, 0b10100101)
        self.assertEqual(self.board.transfers, [((3 << 8 | 0b10100101) << 16) | 0])

    def test_unchanged_rows_are_not_resent(self):
        for row in range(8):
            self.lc.set_row(0, row, eyes.EYE_OPEN[row])
        self.board.transfers.clear()
        for row in range(8):
            self.lc.set_row(0, row, eyes.EYE_OPEN[row])
        self.assertEqual(self.board.transfers, [])
        self.lc.set_row(0, 0, 0)
        self.assertEqual(len(self.board.transfers), 1)


# ─── Render thread ───────────────────────────────────────────────────────────
class BaymaxEyesTests(unittest.TestCase):
    def setUp(self):
        self.shutdown = threading.Event()
        self.eyes = eyes.BaymaxEyes()
        self.eyes.start(self.shutdown)
        self.assertTrue(_wait_for(lambda: self.eyes._board is not None
                                  and self.eyes._lc is not None))
        self.board = self.eyes._board
        # connect() finishes by centring the servo and waiting 0.5 s for it
        self.assertTrue(_wait_for(lambda: self.board.servo))
        time.sleep(0.6)
        self.assertTrue(self.showing(eyes.EYE_OPEN))

    def tearDown(self):
        self.eyes.stop()

    def showing(self, pattern):
        eye1, eye2 = self.board.displayed()
        return eye1 == pattern and eye2 == _flip(pattern)

    def test_starts_open_with_centered_servo(self):
        self.assertTrue(self.showing(eyes.EYE_OPEN))
        self.assertEqual(self.board.servo[0], eyes.SERVO_CENTER)

    def test_thinking_on_and_off(self):
        self.eyes.set_thinking(True)
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_LOOK_UP), timeout=0.5))
        # The squint frame should follow while thinking continues
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_LOOK_UP_SQUINT), timeout=1.5))
        self.eyes.set_thinking(False)
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_OPEN), timeout=0.3))

    def test_thinking_interrupts_a_blink(self):
        self.eyes.blink()
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_CLOSED), timeout=0.5))
        self.eyes.set_thinking(True)
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_LOOK_UP), timeout=0.4))

    def test_no_blinking_while_thinking(self):
        self.eyes.set_thinking(True)
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_LOOK_UP), timeout=0.5))
        self.eyes.blink()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.assertFalse(self.showing(eyes.EYE_CLOSED))
            time.sleep(0.01)

    def test_idle_blinks_on_its_own(self):
        # Random gap is 1.5–4 s, so a blink must happen within ~4.5 s
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_CLOSED), timeout=4.5))
        self.assertTrue(_wait_for(lambda: self.showing(eyes.EYE_OPEN), timeout=1.0))

    def test_shutdown_event_stops_thread_and_clears_eyes(self):
        self.shutdown.set()
        self.eyes._thread.join(timeout=2)
        self.assertFalse(self.eyes._thread.is_alive())
        self.assertTrue(self.showing([0] * 8))


class NoHardwareTests(unittest.TestCase):
    def test_requests_without_thread_are_harmless(self):
        e = eyes.BaymaxEyes()
        e.set_thinking(True)
        e.blink()
        e.set_thinking(False)
        e.stop()

    def test_connection_failure_is_non_fatal(self):
        e = eyes.BaymaxEyes()

        def fail():
            raise OSError("no board")
        e.connect = fail
        e.start()
        e._thread.join(timeout=2)
        self.assertFalse(e._thread.is_alive())


# ─── ThinkingIndicator (fake clock, no threads) ─────────────────────────────
class FakeEyes:
    def __init__(self):
        self.thinking = False
        self.calls = []

    def set_thinking(self, value):
        self.thinking = value
        self.calls.append(value)


class ThinkingIndicatorTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.eyes = FakeEyes()
        self.ind = eyes.ThinkingIndicator(self.eyes, clock=lambda: self.now,
                                          onset=0.35, max_onset=1.5, timeout=8.0)

    def advance(self, seconds, loud=None):
        self.now += seconds
        self.ind.update(last_loud_mic_ts=loud)

    def test_idle_without_user_speech(self):
        self.advance(5)
        self.assertFalse(self.eyes.thinking)

    def test_thinking_after_user_goes_quiet_then_off_on_reply(self):
        self.ind.user_spoke()
        self.advance(0.2)
        self.assertFalse(self.eyes.thinking)
        self.advance(0.2)
        self.assertTrue(self.eyes.thinking)
        self.ind.model_audio()
        self.assertFalse(self.eyes.thinking)

    def test_loud_mic_delays_onset(self):
        self.ind.user_spoke()                      # t=0
        self.advance(0.4, loud=self.now + 0.4)     # t=0.4, mic loud right now
        self.assertFalse(self.eyes.thinking)
        last_loud = self.now
        self.advance(0.2, loud=last_loud)          # quiet 0.2 s
        self.assertFalse(self.eyes.thinking)
        self.advance(0.2, loud=last_loud)          # quiet 0.4 s
        self.assertTrue(self.eyes.thinking)

    def test_noisy_mic_cannot_block_thinking_forever(self):
        self.ind.user_spoke()
        for _ in range(14):
            self.advance(0.1, loud=self.now + 0.1)
        self.assertFalse(self.eyes.thinking)
        self.advance(0.2, loud=self.now + 0.2)     # 1.6 s since last fragment
        self.assertTrue(self.eyes.thinking)

    def test_timeout_returns_to_idle(self):
        self.ind.user_spoke()
        self.advance(0.5)
        self.assertTrue(self.eyes.thinking)
        self.advance(7.0)
        self.assertTrue(self.eyes.thinking)
        self.advance(1.5)
        self.assertFalse(self.eyes.thinking)
        self.advance(1.0)                          # stays idle afterwards
        self.assertFalse(self.eyes.thinking)

    def test_late_transcription_during_reply_is_ignored(self):
        self.ind.user_spoke()
        self.advance(0.5)
        self.ind.model_audio()
        self.ind.user_spoke()                      # arrives after reply started
        self.advance(2.0)
        self.assertFalse(self.eyes.thinking)

    def test_next_turn_after_turn_complete(self):
        self.ind.user_spoke()
        self.advance(0.5)
        self.ind.model_audio()
        self.ind.turn_ended()
        self.ind.user_spoke()
        self.advance(0.5)
        self.assertTrue(self.eyes.thinking)

    def test_interrupted_or_silent_turn_clears_thinking(self):
        self.ind.user_spoke()
        self.advance(0.5)
        self.assertTrue(self.eyes.thinking)
        self.ind.turn_ended()                      # e.g. Gemini chose not to reply
        self.assertFalse(self.eyes.thinking)
        self.advance(1.0)
        self.assertFalse(self.eyes.thinking)

    def test_robot_initiated_turn_never_shows_thinking(self):
        self.ind.model_audio()                     # robot opens the conversation
        self.advance(1.0)
        self.ind.turn_ended()
        self.advance(1.0)
        self.assertNotIn(True, self.eyes.calls)


if __name__ == "__main__":
    unittest.main()
