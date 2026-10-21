"""Delay search, double-talk preservation and diagnostic-only text matching."""
from unittest.mock import Mock, patch
import unittest

import numpy as np

from features.conversational_ability.echo import ResidualEcho, transcript_overlap
from features.conversational_ability.processing import InputEnhancer
from test_conversational_ability import FakeDSP
import runtime_monitor as monitor


class ResidualEchoTests(unittest.TestCase):
    def run_signals(self, delay, user_level=0, shifted=False, echo_gain=.35):
        rate, count = 48000, 48000
        rng = np.random.default_rng(123)
        far = np.clip(rng.normal(0, 2000, count), -10000, 10000).astype(np.int16)
        user = (user_level * np.sin(2*np.pi*223*np.arange(count)/rate)).astype(np.int16)
        delayed = np.concatenate((np.zeros(delay), far[:count-delay])) if delay else far
        if shifted:
            other = 4800
            delayed[count//2:] = far[count//2-other:count-other]
        near = np.clip(delayed * echo_gain + user, -32768, 32767).astype(np.int16)
        clock = [0]
        detector = ResidualEcho(clock=lambda: clock[0])
        output = []
        for offset in range(0, count, 1024):
            clock[0] = offset / rate
            detector.rendered(far[offset:offset+1024].tobytes())
            output.append(detector.process(near[offset:offset+1024], active=True))
        return detector, near, user, np.concatenate(output)

    def test_delayed_echo_is_reduced_across_multiple_distances_and_polarities(self):
        for delay, gain in ((0, .35), (147, -.35), (3840, .35), (10560, -.35)):
            detector, near, _, clean = self.run_signals(delay, echo_gain=gain)
            tail = slice(30000, None)
            ratio = np.sqrt(np.mean(clean[tail].astype(float)**2) / np.mean(near[tail].astype(float)**2))
            self.assertLess(ratio, .2, delay)
            self.assertGreater(detector.metrics['correlation'], .95)

    def test_quiet_independent_user_speech_is_not_muted_by_echo_subtraction(self):
        _, near, user, clean = self.run_signals(3840, user_level=200)
        user, clean = user[30000:].astype(float), clean[30000:].astype(float)
        retained_gain = float(clean @ user / (user @ user))
        self.assertTrue(.8 < retained_gain < 1.2, retained_gain)
        self.assertGreater(np.sqrt(np.mean(clean**2)), 100)

    def test_strong_double_talk_is_passed_unchanged_when_fit_is_uncertain(self):
        detector, near, _, clean = self.run_signals(3840, user_level=4000)
        np.testing.assert_array_equal(clean[30000:], near[30000:])
        self.assertLess(detector.metrics['correlation'], .85)

    def test_delay_change_readapts_without_fixed_geometry(self):
        detector, near, _, clean = self.run_signals(3840, shifted=True)
        ratio = np.sqrt(np.mean(clean[36000:].astype(float)**2) / np.mean(near[36000:].astype(float)**2))
        self.assertLess(ratio, .2)
        self.assertGreater(detector.metrics['correlation'], .95)

    def test_stale_missing_disabled_and_idle_reference_preserve_samples(self):
        clock = [0]
        detector = ResidualEcho(clock=lambda: clock[0])
        samples = np.arange(2048, dtype=np.int16)
        np.testing.assert_array_equal(detector.process(samples, True), samples)
        detector.rendered(samples.tobytes())
        clock[0] = 1
        np.testing.assert_array_equal(detector.process(samples, True), samples)
        np.testing.assert_array_equal(detector.process(samples, False), samples)
        detector.enabled = False
        np.testing.assert_array_equal(detector.process(samples, True), samples)

    def test_reference_is_bounded_and_reset_clears_adaptation(self):
        detector = ResidualEcho()
        detector.rendered(bytes(48000 * 4))
        self.assertLessEqual(len(detector.history), 57600)
        detector.reset()
        self.assertFalse(detector.history)
        self.assertEqual(detector.matches, 0)

    def test_secondary_detector_failure_keeps_healthy_webRTC(self):
        enhancer = InputEnhancer(processor=FakeDSP())
        with patch.object(enhancer.residual, 'process', side_effect=ValueError('FFT failed')):
            output = enhancer.process(np.ones(960, dtype=np.int16))
        self.assertTrue(enhancer.full_duplex)
        self.assertFalse(enhancer.residual.enabled)
        self.assertTrue(np.all(output[480:] == 1))

    def test_distinctive_prefix_overlap_warns_but_short_common_speech_does_not(self):
        robot = ['We can discuss your favorite books after breakfast.']
        self.assertTrue(transcript_overlap('We can discuss your favorite books', robot)['suspected'])
        self.assertFalse(transcript_overlap('yes I agree', ['yes I agree'])['suspected'])
        self.assertFalse(transcript_overlap('Actually I want to change the subject', robot)['suspected'])

    def test_monitor_warning_keeps_user_transcript_and_expires(self):
        with patch.object(monitor, 'transcripts', monitor.deque(maxlen=60)), patch.object(
                monitor, 'active_transcripts', {}), patch.object(monitor, 'echo_text', {'suspected': False}), patch.object(
                monitor, 'event_observer', None), patch.object(monitor, 'waiting_audio', None), patch.object(
                monitor.time, 'time', return_value=100):
            phrase = 'We can discuss your favorite books after breakfast.'
            monitor.transcribe('ember', phrase)
            monitor.transcribe('user', phrase)
            self.assertEqual(monitor.transcripts[-1]['text'], phrase)
            self.assertTrue(monitor.snapshot()['echo_diagnostics']['transcript_match']['suspected'])
            with patch.object(monitor.time, 'time', return_value=109):
                self.assertFalse(monitor.snapshot()['echo_diagnostics']['transcript_match']['suspected'])

    def test_old_robot_speech_and_new_session_do_not_keep_warning(self):
        with patch.object(monitor, 'transcripts', monitor.deque(maxlen=60)), patch.object(
                monitor, 'active_transcripts', {}), patch.object(monitor, 'echo_text', {'suspected': False}), patch.object(
                monitor, 'event_observer', None), patch.object(monitor, 'waiting_audio', None):
            phrase = 'We can discuss your favorite books after breakfast.'
            with patch.object(monitor.time, 'time', return_value=100):
                monitor.transcribe('ember', phrase)
            with patch.object(monitor.time, 'time', return_value=109):
                monitor.transcribe('user', phrase)
                self.assertFalse(monitor.echo_text['suspected'])
            monitor.emit('reconnecting', 'Reconnect')
            self.assertFalse(monitor.echo_text['suspected'])


if __name__ == '__main__':
    unittest.main()
