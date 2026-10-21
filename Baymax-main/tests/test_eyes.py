"""Fake-clock and decoded Firmata-wire checks; no serial device or cloud."""
import unittest
from unittest.mock import Mock, patch

from features.eyes.controller import Eyes, OPEN, BLINK, BLANK, THINKING, eyes_session
from features.eyes.driver import LedControl
from features.eyes.patterns import EXPRESSIONS, SAD, SURPRISED, SLEEP_BASE
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

    def test_all_supplied_expression_frames_are_valid_and_keep_mounting(self):
        self.assertEqual(set(EXPRESSIONS), {'happy', 'sad', 'excited', 'confused', 'surprised', 'curious', 'love', 'sleep', 'dead'})
        self.assertEqual(SAD, (0, 0, 0, 66, 66, 36, 24, 0))
        for name, (frames, seconds) in EXPRESSIONS.items():
            self.assertGreater(seconds, 0)
            for left, right in frames:
                self.assertEqual(len(left), 8)
                self.assertEqual(len(right), 8)
                self.assertTrue(all(0 <= v <= 255 for v in left + right))
        self.assertEqual(len(EXPRESSIONS['confused'][0]), 4)
        for left, right in EXPRESSIONS['sleep'][0]:
            self.assertEqual(left, SLEEP_BASE)
        self.assertNotEqual(*EXPRESSIONS['excited'][0][0])

    def test_expression_survives_tool_finish_speech_and_buffered_completion(self):
        self.eyes.event('user_speech')
        self.assertTrue(self.eyes.express('sad'))
        self.eyes.event('user_speech')  # Late transcription delta for this request.
        self.eyes.event('calling_tool')
        self.eyes.event('thinking')
        self.assertEqual(self.eyes.mode(), 'sad')
        self.now = 3
        self.assertEqual(self.eyes.mode(), 'thinking')
        self.eyes.event('speaking')
        self.assertEqual(self.eyes.mode(), 'sad')
        self.eyes.event('reply_complete')
        self.assertEqual(self.eyes.mode(), 'sad')
        self.eyes.event('reply_playback_done')
        self.assertEqual(self.eyes.mode(), 'normal')

    def test_fall_alert_overrides_model_expression_and_thinking(self):
        self.eyes.express('sad')
        self.eyes.event('fall_alert')
        self.eyes.event('thinking')
        self.now = 3
        self.eyes.express('happy')
        self.assertEqual(self.eyes.mode(), 'surprised')
        self.eyes.event('reply_playback_done')  # No check-in audio has arrived yet.
        self.assertEqual(self.eyes.mode(), 'surprised')
        self.eyes.event('speaking')
        self.eyes.event('reply_complete')
        self.eyes.event('reply_playback_done')
        self.assertEqual(self.eyes.mode(), 'normal')

    def test_expression_expiry_new_user_and_reconnect_do_not_leave_stuck_faces(self):
        for event in ('user_speech', 'listening', 'reconnecting', 'stopping'):
            self.eyes.express('dead')
            self.eyes.event(event)
            self.assertEqual(self.eyes.mode(), 'normal')
        self.eyes.event('listening')
        self.eyes.express('sleep')
        self.now = 31
        self.assertEqual(self.eyes.mode(), 'normal')
        self.eyes.event('fall_alert')
        self.now = 47
        self.assertEqual(self.eyes.mode(), 'normal')
        self.assertFalse(self.eyes.express('not-a-pattern'))
        self.eyes.stopping.set()
        self.assertFalse(self.eyes.express('happy'))

    def test_renderer_plays_supplied_dynamic_frames_in_order(self):
        for name in ('confused', 'sleep', 'excited', 'sad'):
            eyes = Eyes()
            eyes.express(name)
            expected = EXPRESSIONS[name][0]
            frames = []
            def draw(left, right=None):
                frames.append((left, left if right is None else right))
                if len(frames) == len(expected) + 1:
                    eyes.stopping.set()
            with patch.object(eyes, 'hold', return_value=True):
                eyes.render(Mock(draw_pair=draw))
            self.assertEqual(frames, [(OPEN, OPEN), *expected])

    def test_surprise_uses_the_supplied_pattern_and_fast_blink(self):
        eyes = Eyes()
        eyes.express('surprised')
        frames = []
        def draw(left, right=None):
            frames.append(left)
            if len(frames) == 4:
                eyes.stopping.set()
        with patch('features.eyes.controller.random.uniform', return_value=0), patch(
                'features.eyes.controller.random.random', return_value=0.4), patch.object(eyes, 'hold', return_value=True):
            eyes.render(Mock(draw_pair=draw))
        self.assertEqual(frames, [OPEN, SURPRISED, BLINK, SURPRISED])

    def test_monitor_waits_for_playback_drain_without_resetting_waiting_audio(self):
        cue = Mock()
        with patch.object(monitor, 'event_observer', self.eyes.event), patch.object(
                monitor, 'waiting_audio', cue), patch.object(monitor, 'state', 'speaking'), patch.object(monitor, 'detail', ''):
            self.eyes.express('sad')
            self.eyes.event('speaking')
            monitor.emit('listening', 'Reply complete; listening (buffered audio may still play)')
            monitor.snapshot({'playback_bytes': 96000})
            self.assertEqual(self.eyes.mode(), 'sad')
            monitor.snapshot({'playback_bytes': 0})
            self.assertEqual(self.eyes.mode(), 'normal')
        cue.event.assert_called_once_with('reply_complete')

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
