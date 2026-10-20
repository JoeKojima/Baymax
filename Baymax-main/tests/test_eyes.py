"""Fake-clock and decoded Firmata-wire checks; no serial device or cloud."""
import unittest
from unittest.mock import Mock, patch

from features.eyes.controller import Eyes, OPEN, BLINK, BLANK, THINKING, eyes_session
from features.eyes.driver import LedControl
import runtime_monitor as monitor


class Pin:
    def __init__(self, board, number):
        self.board, self.number = board, number

    def write(self, value):
        board = self.board
        board.writes.append((self.number, value))
        board.levels[self.number] = value
        if self.number == 10 and value == 0:
            board.bits = []
        elif self.number == 13 and value == 1 and board.levels.get(10) == 0:
            board.bits.append(board.levels.get(6, 0))
        elif self.number == 10 and value == 1 and board.bits:
            assert len(board.bits) == 32
            words = [int(''.join(map(str, board.bits[i:i+16])), 2) for i in (0, 16)]
            for device, word in zip((1, 0), words):
                if word >> 8:
                    board.registers[device, word >> 8] = word & 255
            board.bits = []


class Board:
    def __init__(self):
        self.levels, self.registers = {}, {}
        self.bits, self.writes, self.pins = [], [], []
        self.closed = False

    def get_pin(self, spec):
        self.pins.append(spec)
        return Pin(self, int(spec.split(':')[1]))

    def exit(self):
        self.closed = True


class EyeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.eyes = Eyes(clock=lambda: self.now)

    def test_thinking_only_strictly_after_two_seconds(self):
        self.eyes.event('user_speech')
        self.eyes.event('thinking')
        for moment in (0, 1.9, 2.0):
            self.now = moment
            self.assertFalse(self.eyes.thinking())
        self.now = 2.001
        self.assertTrue(self.eyes.thinking())

    def test_new_user_fragments_restart_delay_but_state_repeats_do_not(self):
        self.eyes.event('thinking')
        self.now = 1.5
        self.eyes.event('user_speech')
        self.now = 3
        self.eyes.event('thinking')
        self.assertFalse(self.eyes.thinking())
        self.now = 3.501
        self.assertTrue(self.eyes.thinking())

    def test_fast_reply_and_late_transcription_keep_normal_eyes(self):
        self.eyes.event('thinking')
        self.now = 1
        self.eyes.event('speaking')
        self.now = 10
        self.eyes.event('user_speech')
        self.eyes.event('thinking')
        self.assertFalse(self.eyes.thinking())

    def test_listening_interruption_reconnect_and_stop_clear_thinking(self):
        for event in ('listening', 'reconnecting', 'connecting', 'stopping', 'stopped'):
            self.eyes.event('thinking')
            self.now += 3
            self.assertTrue(self.eyes.thinking())
            self.eyes.event(event)
            self.assertFalse(self.eyes.thinking())

    def test_event_requests_do_not_touch_hardware(self):
        factory = Mock()
        eyes = Eyes(board_factory=factory)
        eyes.event('thinking')
        eyes.event('speaking')
        factory.assert_not_called()

    def test_single_dot_moves_across_six_positions(self):
        self.assertEqual(len(THINKING), 6)
        for index, pair in enumerate(THINKING):
            self.assertEqual(sum(v.bit_count() for eye in pair for v in eye), 4)
            self.assertEqual(pair[1 if index < 3 else 0], BLANK)

    def test_wire_protocol_mounting_brightness_and_no_servo(self):
        board = Board()
        leds = LedControl(board)
        leds.set_intensity(0, 3)
        leds.set_intensity(1, 3)
        leds.draw_pair(THINKING[0][0], BLANK)
        self.assertEqual(set(board.pins), {'d:6:o', 'd:13:o', 'd:10:o'})
        for device in (0, 1):
            self.assertEqual(board.registers[device, 15], 0)
            self.assertEqual(board.registers[device, 10], 3)
        self.assertEqual(tuple(board.registers[1, r+1] for r in range(8)), THINKING[0][0])
        self.assertEqual(tuple(board.registers[0, r+1] for r in range(8)), BLANK)
        count = len(board.writes)
        leds.draw_pair(THINKING[0][0], BLANK)
        self.assertEqual(len(board.writes), count)

    def test_failed_row_transfer_is_retried(self):
        leds = LedControl(Board())
        with patch.object(leds, 'transfer', side_effect=[OSError('disconnect'), None]) as transfer:
            with self.assertRaises(OSError):
                leds.set_row(0, 0, 42)
            leds.set_row(0, 0, 42)
        self.assertEqual(transfer.call_count, 2)

    def test_reply_interrupts_thinking_hold_and_restores_open(self):
        self.eyes.event('thinking')
        self.now = 3
        frames = []
        def draw(left, right=None):
            frames.append((left, left if right is None else right))
            if (left, right) == THINKING[0]:
                self.eyes.event('speaking')
            elif len(frames) > 1 and left == OPEN:
                self.eyes.stopping.set()
        self.eyes.render(Mock(draw_pair=draw))
        self.assertEqual(frames, [(OPEN, OPEN), THINKING[0], (OPEN, OPEN)])

    def test_normal_blinks_continue_while_speaking_and_listening(self):
        for event in ('speaking', 'listening'):
            eyes = Eyes()
            eyes.event(event)
            frames = []
            def draw(left, right=None):
                frames.append(left)
                if len(frames) == 3:
                    eyes.stopping.set()
            with patch('features.eyes.controller.random.uniform', return_value=0), patch(
                    'features.eyes.controller.random.random', return_value=0.5), patch.object(eyes, 'hold', return_value=True):
                eyes.render(Mock(draw_pair=draw))
            self.assertEqual(frames, [OPEN, BLINK, OPEN])

    def test_worker_shutdown_blanks_both_and_closes_connection(self):
        board = Board()
        eyes = Eyes(board_factory=lambda port: board)
        eyes.stopping.set()
        eyes.run()
        self.assertTrue(board.closed)
        self.assertEqual(board.registers[0, 12], 0)
        self.assertEqual(board.registers[1, 12], 0)

    def test_missing_hardware_is_nonfatal(self):
        eyes = Eyes(board_factory=Mock(side_effect=OSError('no Arduino')))
        eyes.start()
        eyes.stop()
        self.assertFalse(eyes.thread.is_alive())

    def test_mvp_session_connects_monitor_events_and_restores_observer(self):
        fake = Mock()
        with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '1'}), patch(
                'features.eyes.controller.Eyes', return_value=fake), patch.object(monitor, 'event_observer', None):
            with eyes_session():
                monitor.emit('listening', 'Listening')
                monitor.transcribe('user', 'hello')
                monitor.emit('thinking', 'Waiting')
                monitor.emit('speaking', 'Audio')
            self.assertIsNone(monitor.event_observer)
        self.assertEqual([c.args[0] for c in fake.event.call_args_list],
                         ['listening', 'user_speech', 'thinking', 'speaking'])
        fake.start.assert_called_once()
        fake.stop.assert_called_once()

    def test_disabled_session_does_not_create_hardware(self):
        with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '0'}), patch('features.eyes.controller.Eyes') as factory:
            with eyes_session():
                pass
        factory.assert_not_called()

    def test_observer_failure_cannot_break_monitor_or_transcription(self):
        with patch.object(monitor, 'event_observer', Mock(side_effect=ValueError('bad callback'))):
            monitor.emit('listening', 'Listening')
            self.assertIsNone(monitor.event_observer)


if __name__ == '__main__':
    unittest.main()
